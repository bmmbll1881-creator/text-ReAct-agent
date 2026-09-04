"""ReAct Agent 的运行入口。"""
import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Optional
import httpx
from pydantic import BaseModel
from context import should_compress, compress_conversation
from budget import BudgetExceeded, SessionBusyError, StepBudget
from config import LLM_TIMEOUT, MAX_STEPS, TOOL_TIMEOUT, CONTEXT_TOKEN_LIMIT, CONTEXT_KEEP_RECENT_MESSAGES
from logger import log_event
from parser import parse_response
from prompts import SYSTEM_PROMPT
from tools import TOOL_REGISTRY, execute_tool

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
LLMCallable = Callable[[list[dict[str, str]]], Awaitable[str]]
ToolExecutor = Callable[[str, Any], str]  # 工具执行函数，接收工具名和已验证的模型实例

# 模块级异步HTTP客户端，随应用生命周期复用连接池
_client: Optional[httpx.AsyncClient] = None


class HistoryManager:
    """管理对话历史的追加、压缩和持久化。"""

    def __init__(
        self,
        budget: StepBudget,
        task: str,
        emit: EventCallback,
    ):
        self.budget = budget
        self.task = task
        self.emit = emit

    async def append_and_save(
        self,
        history: list[dict],
        role: str,
        content: str,
    ) -> list[dict]:
        """追加一条消息，自动压缩并保存，返回更新后的历史。"""
        history.append({"role": role, "content": content})
        return await self.save(history)

    async def append_many_and_save(
        self,
        history: list[dict],
        messages: list[dict],
    ) -> list[dict]:
        """追加多条消息，自动压缩并保存，返回更新后的历史。"""
        history.extend(messages)
        return await self.save(history)

    async def save(self, history: list[dict]) -> list[dict]:
        """保存历史前自动检查并压缩。"""
        history = await self._compress_if_needed(history)
        self.budget.save_history(history)
        return history

    async def _compress_if_needed(self, history: list[dict]) -> list[dict]:
        """如果需要压缩则压缩，并发出事件。"""
        if should_compress(history, CONTEXT_TOKEN_LIMIT):
            old_cnt = len(history)
            try:
                history = await compress_conversation(
                    history,
                    call_llm,
                    self.task,
                    SYSTEM_PROMPT,
                    CONTEXT_KEEP_RECENT_MESSAGES,
                )
                new_cnt = len(history)
                await self.emit(
                    "context_compressed",
                    {"before": old_cnt, "after": new_cnt},
                )
            except Exception as e:
                # 压缩失败时降级，保留原历史
                await self.emit(
                    "context_compression_error",
                    {"error": str(e)},
                )
        return history

class MaxStepsExceeded(Exception):
    """步骤超限异常。"""


def get_http_client() -> httpx.AsyncClient:  # httpx.AsyncClient 的构造是同步的，因为采用懒连接池，所以这里采用同步方式
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT))
    return _client


def _should_retry_error(exc: BaseException) -> bool:
    """判断异常是否需要重试。仅对网络错误、超时和 5xx/429 状态码重试。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
    return False


async def call_llm(messages: list[dict], system_prompt: str | None = None) -> str:
    """异步调用deepseek， 带重试，超时"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    url = os.getenv("CHAT_URL")

    system_prompt = system_prompt + "\n" + SYSTEM_PROMPT if system_prompt else SYSTEM_PROMPT

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"
               }
    data = {
        "model": "deepseek-chat",
        "system_prompt": system_prompt,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        "temperature": 0.0,
    }

    # 使用连接池，避免每次请求都创建连接
    client = get_http_client()
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()

            # 解析json
            try:
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]

                # 记录token使用(如果存在)
                usage = payload.get("usage")
                if usage:
                    log_event("llm_token_usage", "", None,
                              prompt_tokens=usage.get("prompt_tokens"),
                              completion_tokens=usage.get("completion_tokens"))

                return content

            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
                # 解析响应的 JSON 出现的格式错误， 不重试
                raise RuntimeError("DeepSeek 返回内容格式错误") from error

        except (httpx.ConnectError, httpx.ReadTimeout) as error:
            # 连接错误或读取超时重试
            await _retry(attempt, error, max_attempts)

        except httpx.HTTPStatusError as error:
            # HTTP 状态码错误重试
            status = error.response.status_code
            if status in (429, 500, 502, 503, 504):
                await _retry(attempt, error, max_attempts)
            else:
                # 其他 HTTP 错误，不重试
                raise

    # 兜底，理论上不会执行到这里
    raise RuntimeError("LLM调用失败")


async def _retry(attempt: int, error, max_attempts: int):
    """重试逻辑，避免重复代码。"""
    if attempt == max_attempts:
        raise
    wait = 2 ** attempt
    log_event("llm_retry", "", f"step:{attempt}", reason=str(error), wait=wait)
    await asyncio.sleep(wait)


def _validate_max_steps(max_steps: int | None) -> int:
    """解析并校验最大步数，返回实际使用的步数上限。"""
    steps = MAX_STEPS if max_steps is None else max_steps
    if steps < 1:
        raise ValueError("max_steps 至少应为 1")
    return steps


def _load_history(budget: StepBudget, task: str) -> list[dict]:
    """加载会话历史：新会话初始化为系统提示+用户任务，旧会话按需补充系统提示。"""
    history = budget.load_history()
    if not history:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
    if not any(m.get("role") == "system" for m in history):
        history.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    return history


def _consume_step(budget: StepBudget) -> None:
    """消耗一步预算，预算超限时转换为 MaxStepsExceeded。"""
    try:
        budget.consume()
    except BudgetExceeded as error:
        raise MaxStepsExceeded(str(error)) from error


def _validate_tool_input(model_cls: BaseModel, tool_input: Any) -> Any:
    """把 action_input 解析并校验为工具入参模型实例。"""
    if isinstance(tool_input, str):
        return model_cls.model_validate_json(tool_input)
    return model_cls.model_validate(tool_input)


async def _execute_action(
        result: Any,
        tool_executor: ToolExecutor,
        emit: EventCallback,
) -> str:
    """执行 LLM 输出中解析出的动作，返回 observation 文本。"""
    if not result.action:
        observation = "未能解析出有效动作，请按格式重新输出。"
        await emit("parse_error", {"error": observation})
        return observation

    tool_name, tool_input = result.action, result.action_input
    tool_meta = TOOL_REGISTRY.get(tool_name)
    if not tool_meta:
        observation = f"工具不存在: 工具 {tool_name} 不存在"
        await emit("tool_error", {"error": observation})
        return observation

    try:
        validated_input = _validate_tool_input(tool_meta["model"], tool_input)
    except Exception as error:
        observation = f"输入验证错误: {str(error)}"
        await emit("tool_error", {"error": observation})
        return observation

    try:
        tool_result = await asyncio.wait_for(
            asyncio.to_thread(tool_executor, tool_name, validated_input),
            TOOL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        observation = f"工具调用超时: 工具 {tool_name} 超时"
        await emit("tool_error", {"error": observation})
        return observation
    except Exception as error:
        observation = f"工具调用错误: {str(error)}"
        await emit("tool_error", {"error": observation})
        return observation

    observation = f"工具输出: {tool_result}"
    await emit("tool_call", {"tool_name": tool_name, "tool_input": tool_input})
    await emit("tool_output", {"output": observation})
    return observation

async def run_agent(
        task: str,
        session_id: str,
        *,
        max_steps: int | None = None,
        on_event: EventCallback | None = None,
        llm_call: LLMCallable = call_llm,
        tool_executor: ToolExecutor = execute_tool,
        budget: StepBudget | None = None,
) -> str:
    """执行一个 ReAct 任务；依赖可注入，便于测试和嵌入其他应用。"""
    max_steps = _validate_max_steps(max_steps)
    budget = budget or StepBudget(session_id, max_steps)
    history = _load_history(budget, task)

    async def emit(event_type: str, data: dict[str, Any]) -> None:
        if on_event:
            await on_event(event_type, data)

    history_manager = HistoryManager(budget, task, emit)

    try:
        await asyncio.to_thread(budget.acquire_lock)
    except SessionBusyError as error:
        raise RuntimeError("会话正忙，请稍后重试") from error

    try:
        for step in range(1, max_steps + 1):
            _consume_step(budget)
            await emit("step_start", {"step": step})

            try:
                response_text = await llm_call(history)
            except Exception as error:
                await emit("llm_error", {"error": str(error)})
                history = await history_manager.append_many_and_save(
                    history,
                    [{"role": "assistant", "content": f"LLM错误: {str(error)}"},
                    {"role": "user", "content": "continue the task"}]
                )
                continue

            history = await history_manager.append_and_save(history, "assistant", response_text)

            result = parse_response(response_text)
            if result.thought:
                await emit("thought", {"thought": result.thought})
            if result.final_answer:
                await emit("final_answer", {"answer": result.final_answer})
                return result.final_answer

            observation = await _execute_action(result, tool_executor, emit)
            history = await history_manager.append_and_save(history, "user", observation)

        raise MaxStepsExceeded(
            f"达到最大步数 {max_steps}，请增加最大步数或优化任务。"
        )

    finally:
        await asyncio.to_thread(budget.release_lock)


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="运行文本处理 ReAct Agent")
    cli.add_argument("task", help="需要完成的任务")
    cli.add_argument("--session-id", required=True, help="Redis 中的会话标识")
    cli.add_argument("--max-steps", type=int, default=MAX_STEPS, help="最大执行步数")
    args = cli.parse_args()
    run = asyncio.run(run_agent(args.task, args.session_id, max_steps=args.max_steps))
    print(run)

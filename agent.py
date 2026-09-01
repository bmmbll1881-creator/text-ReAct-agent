"""ReAct Agent 的运行入口。"""

import os

import requests
from dotenv import load_dotenv

from budget import BudgetExceeded, StepBudget
from parser import parse_response
from prompts import SYSTEM_PROMPT
from tools import execute_tool

load_dotenv()

MAX_STEPS = 8
CHAT_URL = "https://api.deepseek.com/chat/completions"


def call_llm(messages: list[dict], *, http_post=requests.post) -> str:
    """将当前对话发送给 DeepSeek，并返回模型文本。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        "temperature": 0.0,
    }
    response = http_post(url=CHAT_URL, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("DeepSeek 返回内容缺少 choices[0].message.content") from error


def run_agent(
    task: str,
    session_id: str,
    *,
    max_steps: int = MAX_STEPS,
    llm_call=call_llm,
    tool_executor=execute_tool,
    budget=None,
) -> str:
    """执行一个 ReAct 任务；依赖可注入，便于测试和嵌入其他应用。"""
    if max_steps < 1:
        raise ValueError("max_steps 至少应为 1")

    messages = [{"role": "user", "content": task}]
    step_budget = budget or StepBudget(session_id, max_steps)

    for step in range(1, max_steps + 1):
        try:
            current = step_budget.consume()
        except BudgetExceeded as error:
            raise RuntimeError(str(error)) from error

        print(f"第 {step}/{max_steps} 步，当前会话累计：{current}")
        response = llm_call(messages)
        messages.append({"role": "assistant", "content": response})
        result = parse_response(response)

        if result.final_answer:
            print(f"最终答复：{result.final_answer}")
            return result.final_answer

        if result.error:
            observation = f"解析失败：{result.error}。请按约定格式重新输出。"
        else:
            try:
                observation = f"工具执行成功：{tool_executor(result.action, result.action_input)}"
            except Exception as error:
                observation = f"工具执行失败：{type(error).__name__}: {error}"

        print(f"观察结果：{observation}")
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    raise RuntimeError(f"步骤已达上限：max_steps={max_steps}，任务尚未完成")


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="运行文本处理 ReAct Agent")
    cli.add_argument("task", help="需要完成的任务")
    cli.add_argument("--session-id", required=True, help="Redis 中的会话标识")
    cli.add_argument("--max-steps", type=int, default=MAX_STEPS, help="最大执行步数")
    args = cli.parse_args()
    print(run_agent(args.task, args.session_id, max_steps=args.max_steps))

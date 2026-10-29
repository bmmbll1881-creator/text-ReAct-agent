"""异步工具链测试：协程分流、observation 格式化、注册表分层与提示词生成。"""

import asyncio
import json

import pytest

import agent
import prompts
import tool_dispatch
from budget import BudgetExceeded
from prompts import build_system_prompt
from tools import MCP_TOOL_SPECS, ReadFileInput, WriteFileInput, execute_tool


class FakeBudget:
    """最小可用预算替身，避免测试依赖 Redis。"""

    def __init__(self, limit: int = 10):
        self.limit = limit
        self.max_steps = limit
        self.current = 0
        self.locked = False

    def consume(self) -> int:
        self.current += 1
        if self.current > self.limit:
            raise BudgetExceeded("budget exhausted")
        return self.current

    def load_history(self) -> list:
        return []

    def save_history(self, messages) -> None:
        pass

    def acquire_lock(self) -> None:
        self.locked = True

    def release_lock(self) -> None:
        self.locked = False


# ---------------------------------------------------------------- 协程分流


@pytest.mark.asyncio
async def test_async_tool_executor_result_reaches_observation():
    """异步执行器必须被 await，不能以 coroutine 对象的形式进入历史。"""
    seen: list[str] = []

    async def llm(messages, _skill=None, _session="", _step=None):
        seen.append(messages[-1]["content"])
        if len(seen) == 1:
            return 'Action: read_file\nAction Input: {"path": "x.txt"}'
        return "Final Answer: done"

    async def tool(name, payload):
        await asyncio.sleep(0)
        return "async-output"

    assert await run("async", llm, tool) == "done"
    assert seen[1] == "工具输出: async-output"
    assert "coroutine" not in seen[1]


@pytest.mark.asyncio
async def test_sync_tool_executor_still_supported():
    """同步执行器（现有测试与旧调用方）继续可用。"""
    async def llm(_messages, _skill=None, _session="", _step=None):
        if not ran:
            ran.append(True)
            return 'Action: read_file\nAction Input: {"path": "x.txt"}'
        return "Final Answer: ok"

    ran: list[bool] = []
    assert await run("sync", llm, lambda *_: "sync-output") == "ok"


@pytest.mark.asyncio
async def test_awaitable_returned_from_thread_is_awaited():
    """partial / 可调用对象包装的协程不会被漏掉。"""
    class ReturnsCoroutine:
        def __call__(self, _name, _payload):
            return asyncio.sleep(0, result="wrapped")

    result = await agent._run_tool(ReturnsCoroutine(), "read_file", object(), 1.0)
    assert result == "wrapped"


@pytest.mark.asyncio
async def test_async_tool_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(agent, "TOOL_TIMEOUT", 0.01)

    async def llm(messages, _skill=None, _session="", _step=None):
        if messages[-1]["role"] == "user" and "工具" not in messages[-1]["content"]:
            return 'Action: read_file\nAction Input: {"path": "x.txt"}'
        return "Final Answer: timed out"

    async def slow(*_):
        await asyncio.sleep(0.2)

    assert await run("timeout", llm, slow) == "timed out"


@pytest.mark.asyncio
async def test_async_tool_error_becomes_observation():
    async def llm(messages, _skill=None, _session="", _step=None):
        if messages[-1]["role"] == "user" and "错误" not in messages[-1]["content"]:
            return 'Action: read_file\nAction Input: {"path": "x.txt"}'
        assert "工具调用错误: boom" in messages[-1]["content"]
        return "Final Answer: handled"

    async def boom(*_):
        raise RuntimeError("boom")

    assert await run("error", llm, boom) == "handled"


# ---------------------------------------------------------------- 分派层


@pytest.mark.asyncio
async def test_execute_tool_async_runs_local_tool(workspace):
    result = await tool_dispatch.execute_tool_async(
        "write_file", WriteFileInput(path="note.txt", content="hello")
    )

    assert "note.txt" in result
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_execute_tool_async_rejects_unknown_tool():
    with pytest.raises(ValueError, match="未知工具"):
        await tool_dispatch.execute_tool_async("delete_file", ReadFileInput(path="x.txt"))


def test_local_registry_rejects_mcp_tool(monkeypatch):
    """MCP 工具只在合并视图里出现，同步入口必须给出明确指引。"""
    monkeypatch.setitem(MCP_TOOL_SPECS, "knowledge_search", {"model": ReadFileInput})

    with pytest.raises(ValueError, match="必须通过 execute_tool_async 调用"):
        execute_tool("knowledge_search", ReadFileInput(path="x.txt"))


# ---------------------------------------------------------------- observation


def test_format_observation_keeps_plain_text_shape():
    assert tool_dispatch.format_observation("ok") == "工具输出: ok"
    assert tool_dispatch.format_observation("x" * 10, limit=4) == "工具输出: xxxx\n…(已截断)"


def test_format_observation_serializes_dict_as_json():
    payload = {"status": "success", "answer": "营收 100 万 [S1]", "citations": ["S1"]}

    text = tool_dispatch.format_observation(payload)

    assert text.startswith("工具输出(JSON): ")
    assert json.loads(text.removeprefix("工具输出(JSON): ")) == payload
    assert "\\u" not in text  # 中文不被转义


def test_format_observation_shrinks_evidence_first():
    """超长时先裁剪 evidence 条数与预览，保留可解析的 JSON。"""
    payload = {
        "status": "success",
        "answer": "答" * 2000,
        "evidence": [{"citation_id": f"S{i}", "preview": "证" * 300} for i in range(5)],
    }

    text = tool_dispatch.format_observation(payload, limit=1200)
    body = json.loads(text.removeprefix("工具输出(JSON): "))

    assert body["truncated"] is True
    assert len(body["evidence"]) == 2
    assert len(body["evidence"][0]["preview"]) <= 81
    assert len(body["answer"]) <= 801


def test_format_observation_falls_back_to_skeleton():
    """预算很紧时清空 evidence，只留标量字段，且仍是合法 JSON。"""
    payload = {
        "status": "abstain",
        "route": "abstain",
        "run_id": "run_1",
        "failure_type": "insufficient_coverage",
        "knowledge_base": "contract",
        "answer": "答" * 2000,
        "citations": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"],
        "message": "当前知识库没有足够证据支持该问题",
        "evidence": [{"citation_id": f"S{i}", "preview": "证" * 300} for i in range(5)],
    }

    text = tool_dispatch.format_observation(payload, limit=300)
    body = json.loads(text.removeprefix("工具输出(JSON): "))

    assert len(text) <= 300 + len("工具输出(JSON): ") + 1
    assert body["truncated"] is True
    assert body["status"] == "abstain"
    assert body["failure_type"] == "insufficient_coverage"
    assert len(body["citations"]) <= 6
    assert "evidence" not in body


def test_format_observation_hard_truncates_when_nothing_fits():
    payload = {"answer": "a" * 500}

    text = tool_dispatch.format_observation(payload, limit=20)

    assert text.endswith("…(已截断)")


# ---------------------------------------------------------------- 提示词


def test_system_prompt_lists_registered_tools():
    prompt = build_system_prompt()

    assert "read_file" in prompt and "write_file" in prompt
    assert "输入：path（必填）" in prompt
    assert "knowledge_search" not in prompt
    assert "知识库规则" not in prompt


def test_system_prompt_keeps_single_braces_in_format_example():
    """模板用的是 str.replace，不是 str.format，双花括号会原样泄漏给模型。"""
    prompt = build_system_prompt()

    assert 'Action Input: {"键": "值"}' in prompt
    assert "{{" not in prompt and "}}" not in prompt


def test_system_prompt_adds_knowledge_rules_when_mcp_tool_registered(monkeypatch):
    monkeypatch.setitem(
        MCP_TOOL_SPECS,
        "knowledge_search",
        {"model": ReadFileInput, "description": "查询知识库", "input_schema": {}},
    )
    # 注册表按需变化，缓存必须失效后重新生成
    prompts.build_system_prompt.cache_clear()
    try:
        prompt = build_system_prompt()
        assert "knowledge_search" in prompt
        assert "知识库规则" in prompt
        assert "[S1]" in prompt
    finally:
        prompts.build_system_prompt.cache_clear()


# ---------------------------------------------------------------- 辅助


async def run(task: str, llm, tool_executor) -> str:
    """用替身预算运行一个任务，避免依赖 Redis。"""
    return await agent.run_agent(
        task, "s", max_steps=4, llm_call=llm, tool_executor=tool_executor,
        budget=FakeBudget(),
    )

import pytest

from agent import call_llm, run_agent
from budget import BudgetExceeded


class FakeBudget:
    def __init__(self, limit=10):
        self.limit = limit
        self.current = 0

    def consume(self):
        self.current += 1
        if self.current > self.limit:
            raise BudgetExceeded("budget exhausted")
        return self.current


def test_completes_react_workflow_and_returns_final_answer():
    replies = iter([
        'Thought: write\nAction: write_file\nAction Input: {"path": "note.txt", "content": "hello"}',
        "Thought: complete\nFinal Answer: note created",
    ])
    calls = []

    result = run_agent(
        "create a note", "test-session", max_steps=3,
        llm_call=lambda messages: next(replies),
        tool_executor=lambda name, payload: calls.append((name, payload)) or "written",
        budget=FakeBudget(),
    )

    assert result == "note created"
    assert calls == [("write_file", {"path": "note.txt", "content": "hello"})]


def test_feeds_tool_error_back_to_model():
    messages_seen = []
    replies = iter(['Action: read_file\nAction Input: {"path": "missing.txt"}', "Final Answer: unavailable"])

    def llm(messages):
        messages_seen.append(messages.copy())
        return next(replies)

    def broken_tool(name, payload):
        raise FileNotFoundError("missing.txt")

    assert run_agent("read", "test", llm_call=llm, tool_executor=broken_tool, budget=FakeBudget()) == "unavailable"
    assert "工具执行失败：FileNotFoundError: missing.txt" in messages_seen[1][-1]["content"]


def test_stops_at_configured_step_limit():
    with pytest.raises(RuntimeError, match="max_steps=2"):
        run_agent("loop", "test", max_steps=2,
                  llm_call=lambda messages: 'Action: read_file\nAction Input: {"path": "x.txt"}',
                  tool_executor=lambda name, payload: "ok", budget=FakeBudget())


def test_call_llm_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="未配置 DEEPSEEK_API_KEY"):
        call_llm([])


def test_rejects_non_positive_step_limit():
    with pytest.raises(ValueError, match="至少应为 1"):
        run_agent("task", "session", max_steps=0)

import asyncio
import httpx
import pytest
import agent
from agent import MaxStepsExceeded, call_llm, run_agent
from budget import BudgetExceeded

class FakeBudget:
    def __init__(self, limit=10):
        self.limit, self.max_steps = limit, limit
        self.current, self.locked, self.saved = 0, False, []
    def consume(self):
        self.current += 1
        if self.current > self.limit: raise BudgetExceeded("budget exhausted")
        return self.current
    def load_history(self): return []
    def save_history(self, messages): self.saved.append(messages)
    def acquire_lock(self): self.locked = True
    def release_lock(self): self.locked = False

@pytest.mark.asyncio
async def test_normal_multistep_read_write_workflow():
    replies = iter(['Action: write_file\nAction Input: {"path":"note.txt","content":"hello"}', 'Action: read_file\nAction Input: {"path":"note.txt"}', "Final Answer: hello"])
    async def llm(_, __=None, ___="", ____=None): return next(replies)
    calls = []
    def tool(name, payload): calls.append((name, payload)); return "ok"
    assert await run_agent("task", "s", max_steps=4, llm_call=llm, tool_executor=tool, budget=FakeBudget()) == "hello"
    assert [x[0] for x in calls] == ["write_file", "read_file"]

@pytest.mark.asyncio
async def test_parse_failure_recovers():
    replies = iter(["Thought: malformed", "Final Answer: recovered"])
    async def llm(messages, __=None, ___="", ____=None):
        if len(messages) > 2: assert "解析" in messages[-1]["content"]
        return next(replies)
    assert await run_agent("task", "s", llm_call=llm, budget=FakeBudget()) == "recovered"

@pytest.mark.asyncio
async def test_tool_validation_failure_is_recoverable():
    replies = iter(['Action: read_file\nAction Input: {"path":"x.txt","extra":1}', "Final Answer: fixed"])
    events = []
    async def llm(_, __=None, ___="", ____=None): return next(replies)
    async def emit(event, data): events.append((event, data))
    assert await run_agent("task", "s", llm_call=llm, on_event=emit, budget=FakeBudget()) == "fixed"
    assert any(e == "tool_error" for e, _ in events)

@pytest.mark.asyncio
async def test_tool_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(agent, "TOOL_TIMEOUT", 0.01)
    replies = iter(['Action: read_file\nAction Input: {"path":"x.txt"}', "Final Answer: timed out"])
    async def llm(_, __=None, ___="", ____=None): return next(replies)
    def slow(*_):
        import time; time.sleep(.1)
    assert await run_agent("task", "s", llm_call=llm, tool_executor=slow, budget=FakeBudget()) == "timed out"

@pytest.mark.asyncio
async def test_reaches_max_steps():
    async def llm(_, __=None, ___="", ____=None): return 'Action: read_file\nAction Input: {"path":"x.txt"}'
    with pytest.raises(MaxStepsExceeded):
        await run_agent("loop", "s", max_steps=2, llm_call=llm,
                        tool_executor=lambda *_: "ok", budget=FakeBudget())

@pytest.mark.asyncio
async def test_existing_budget_limit_wins_over_conflicting_max_steps():
    async def llm(_, __=None, ___="", ____=None):
        return 'Action: read_file\nAction Input: {"path":"x.txt"}'
    budget = FakeBudget(limit=1)
    with pytest.raises(MaxStepsExceeded):
        await run_agent("loop", "s", max_steps=3, llm_call=llm,
                        tool_executor=lambda *_: "ok", budget=budget)
    assert budget.current == 1

@pytest.mark.asyncio
async def test_cancelled_run_releases_lock():
    async def llm(_, __=None, ___="", ____=None): raise asyncio.CancelledError()
    budget = FakeBudget()
    with pytest.raises(asyncio.CancelledError): await run_agent("task", "s", llm_call=llm, budget=budget)
    assert not budget.locked

@pytest.mark.asyncio
async def test_call_llm_retries_transient_errors(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key"); monkeypatch.setenv("CHAT_URL", "https://example.test")
    class Client:
        calls = 0
        async def post(self, *_a, **_k):
            self.calls += 1
            if self.calls < 3: raise httpx.ReadTimeout("temporary")
            return httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                                 json={"choices":[{"message":{"content":"ok"}}]})
    client = Client(); monkeypatch.setattr(agent, "get_http_client", lambda: client)
    async def instant_sleep(_): return None
    monkeypatch.setattr(agent.asyncio, "sleep", instant_sleep)
    assert await call_llm([]) == "ok" and client.calls == 3

@pytest.mark.asyncio
async def test_call_llm_does_not_retry_permanent_error(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key"); monkeypatch.setenv("CHAT_URL", "https://example.test")
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.test"))
    class Client:
        calls = 0
        async def post(self, *_a, **_k): self.calls += 1; return response
    client = Client(); monkeypatch.setattr(agent, "get_http_client", lambda: client)
    with pytest.raises(httpx.HTTPStatusError): await call_llm([])
    assert client.calls == 1

@pytest.mark.asyncio
async def test_call_llm_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"): await call_llm([])


@pytest.mark.asyncio
async def test_events_are_logged_with_session_step_and_timing(monkeypatch):
    logged = []
    monkeypatch.setattr(agent, "log_event", lambda event, session, step=None, **extra: logged.append((event, session, step, extra)))
    async def llm(_, __=None, ___="", ____=None): return "Final Answer: done"
    await run_agent("task", "session-1", max_steps=1, llm_call=llm, budget=FakeBudget())
    assert any(event == "step_start" and session == "session-1" and step == 1 for event, session, step, _ in logged)
    complete = next(extra for event, _, _, extra in logged if event == "llm_complete")
    assert complete["duration_ms"] >= 0

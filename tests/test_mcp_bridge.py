"""MCP 桥接层测试：约束注入、事件、错误降级与 API 生命周期。"""

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

import agent
import api
import knowledge_backend
import tool_dispatch
from budget import BudgetExceeded
from knowledge_backend import McpKnowledgeBackend, StubKnowledgeBackend
from mcp_client import McpToolError

SUCCESS_PAYLOAD = {
    "status": "success",
    "knowledge_base": "contract",
    "answer": "任一方提前终止合同，应按照合同约定承担违约责任。 [S1]",
    "citations": ["S1"],
    "route": "success",
    "run_id": "run_ok",
    "failure_type": None,
}

ABSTAIN_PAYLOAD = {
    "status": "abstain",
    "knowledge_base": "contract",
    "answer": None,
    "citations": [],
    "route": "abstain",
    "run_id": "run_abstain",
    "failure_type": "insufficient_coverage",
}


class FakeBudget:
    """最小预算替身，避免依赖 Redis。"""

    def __init__(self, limit: int = 10):
        self.limit = limit
        self.max_steps = limit
        self.current = 0

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
        pass

    def release_lock(self) -> None:
        pass


async def _run_with_backend(backend, *, final: str = "Final Answer: done") -> tuple[str, list, list]:
    """用给定 backend 跑一次「先查知识库再作答」的任务。"""
    knowledge_backend.set_backend(backend)
    observations: list[str] = []
    events: list[tuple[str, dict]] = []

    async def llm(messages, _skill=None, _session="", _step=None):
        observations.append(messages[-1]["content"])
        if len(observations) == 1:
            return 'Action: knowledge_search\nAction Input: {"question": "违约金多少"}'
        return final

    async def on_event(event: str, data: dict) -> None:
        events.append((event, data))

    try:
        answer = await agent.run_agent(
            "kb", "s", max_steps=3, llm_call=llm, on_event=on_event, budget=FakeBudget()
        )
    finally:
        knowledge_backend.set_backend(None)
    return answer, observations, events


# ---------------------------------------------------------------- 约束注入


@pytest.mark.asyncio
async def test_abstain_injects_runtime_constraint():
    answer, observations, _ = await _run_with_backend(StubKnowledgeBackend(ABSTAIN_PAYLOAD))

    observation = observations[1]
    assert "[系统约束]" in observation
    assert "不得基于常识或推测补充答案" in observation
    assert "run_abstain" in observation
    assert answer == "done"


@pytest.mark.asyncio
async def test_success_has_no_constraint():
    _, observations, _ = await _run_with_backend(StubKnowledgeBackend(SUCCESS_PAYLOAD))

    observation = observations[1]
    assert "[系统约束]" not in observation
    assert "[S1]" in observation


@pytest.mark.asyncio
async def test_disabled_backend_injects_disabled_constraint():
    _, observations, _ = await _run_with_backend(None)

    assert '"status": "disabled"' in observations[1]
    assert "知识库工具未启用" in observations[1]


@pytest.mark.parametrize(
    ("status", "keyword"),
    [
        ("clarify", "先向用户确认"),
        ("verification_failed", "未通过校验"),
        ("error", "如实说明原因"),
    ],
)
def test_every_non_success_status_has_constraint(status, keyword):
    note = tool_dispatch.constraint_for({"status": status})

    assert note.startswith("[系统约束]")
    assert keyword in note


def test_success_and_non_dict_have_no_constraint():
    assert tool_dispatch.constraint_for({"status": "success"}) == ""
    assert tool_dispatch.constraint_for("plain text") == ""


# ---------------------------------------------------------------- SSE 事件


@pytest.mark.asyncio
async def test_knowledge_result_event_carries_trace_fields():
    _, _, events = await _run_with_backend(StubKnowledgeBackend(SUCCESS_PAYLOAD))

    payload = next(data for event, data in events if event == "knowledge_result")
    assert payload == {
        "knowledge_base": "contract",
        "status": "success",
        "route": "success",
        "run_id": "run_ok",
        "citations": ["S1"],
        "failure_type": None,
    }


@pytest.mark.asyncio
async def test_tool_error_emits_no_knowledge_result():
    _, _, events = await _run_with_backend(StubKnowledgeBackend(McpToolError("boom")))

    assert any(event == "tool_error" for event, _ in events)
    assert not any(event == "knowledge_result" for event, _ in events)


# ---------------------------------------------------------------- 错误与超时


@pytest.mark.asyncio
async def test_mcp_tool_error_becomes_observation():
    _, observations, _ = await _run_with_backend(StubKnowledgeBackend(McpToolError("server down")))

    assert "工具调用错误" in observations[1]
    assert "server down" in observations[1]


@pytest.mark.asyncio
async def test_mcp_timeout_uses_dedicated_budget(monkeypatch):
    """MCP 工具走 RAG_MCP_TOOL_TIMEOUT，而不是本地 TOOL_TIMEOUT。"""
    monkeypatch.setattr(agent, "RAG_MCP_TOOL_TIMEOUT", 0.01)
    monkeypatch.setattr(agent, "TOOL_TIMEOUT", 30.0)
    backend = StubKnowledgeBackend(SUCCESS_PAYLOAD, delay=0.2)

    _, observations, _ = await _run_with_backend(backend)

    assert "工具调用超时" in observations[1]


# ---------------------------------------------------------------- API 生命周期


def test_health_reports_disabled_when_mcp_off(monkeypatch):
    monkeypatch.setattr(api, "RAG_MCP_ENABLED", False)

    with TestClient(api.app) as client:
        response = client.get("/health")

    assert response.json() == {"status": "ok", "mcp": "disabled"}
    assert knowledge_backend.get_backend().__class__.__name__ == "NullKnowledgeBackend"


def test_lifespan_starts_and_closes_backend(monkeypatch):
    class FakeClient:
        def __init__(self) -> None:
            self.started = False
            self.closed = False

        async def start(self) -> None:
            self.started = True

        async def close(self) -> None:
            self.closed = True

        async def call_tool(self, name, arguments, *, timeout=60):
            return {"status": "success", "answer": "ok"}

    fake = FakeClient()
    monkeypatch.setattr(api, "RAG_MCP_ENABLED", True)
    monkeypatch.setattr(api, "client_from_config", lambda settings: fake)

    with TestClient(api.app) as client:
        assert client.get("/health").json()["mcp"] == "ready"
        assert fake.started
        assert isinstance(knowledge_backend.get_backend(), McpKnowledgeBackend)

    assert fake.closed
    assert knowledge_backend.get_backend().__class__.__name__ == "NullKnowledgeBackend"


def test_lifespan_degrades_when_mcp_startup_fails(monkeypatch):
    """MCP 起不来时服务照常存活，health 明确报告 error。"""
    monkeypatch.setattr(api, "RAG_MCP_ENABLED", True)

    def boom(settings):
        raise RuntimeError("python 路径不存在")

    monkeypatch.setattr(api, "client_from_config", boom)

    with TestClient(api.app) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["mcp"].startswith("error:")
    assert "python 路径不存在" in body["mcp"]


# ---------------------------------------------------------------- 真集成（默认跳过）


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RAG_INTEGRATION") != "1",
    reason="设置 RAG_INTEGRATION=1 才启动真实 MCP 子进程",
)
def test_real_mcp_matches_shared_case_file():
    """真实 stdio 子进程 + 两端共用的 cases.json：答案事实与引用必须一致。"""
    import re
    from pathlib import Path

    import config as settings
    from mcp_client import client_from_config

    project = settings.RAG_MCP_PROJECT
    assert project is not None
    cases_path = Path(project) / "demo" / "cases.json"
    if not cases_path.exists():
        pytest.skip(f"缺少共用用例文件：{cases_path}")

    payload_cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    case = next(item for item in payload_cases if item["id"] == "finance_revenue")

    client = client_from_config(settings.config)

    async def call() -> dict:
        await client.start()
        try:
            return await client.call_tool(
                "knowledge_search",
                {"question": case["question"], "knowledge_base": case["knowledge_base"]},
                timeout=settings.RAG_MCP_TOOL_TIMEOUT,
            )
        finally:
            await client.close()

    payload = asyncio.run(call())

    assert payload["status"] == case["expected_route"]
    expected_fragment = re.sub(r"\s*\[S\d+\]", "", case["expected_answer"]).strip()
    assert expected_fragment in re.sub(r"\s*\[S\d+\]", "", payload["answer"]).strip()
    assert payload["citations"] == case["expected_citations"]

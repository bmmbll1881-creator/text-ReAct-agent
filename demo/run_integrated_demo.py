"""联合联调 Demo：ReAct Agent + MCP 知识库（evidence-reflective-trace-rag）。

数据来源统一：用例与文档定义都读 RAG 仓库的 ``demo/cases.json``，
与 RAG 侧 ``seed_demo`` / ``run_demo`` / ``test_demo`` 完全相同，避免两端预期漂移。

分两段执行：

A. **链路自检**（不需要 LLM）：逐条调 ``knowledge_search``，核对 status / 答案事实 / 引用。
B. **端到端**（需要 DEEPSEEK_API_KEY）：ReAct 自主编排 5 个任务，验证引用保留、
   拒答不编造、陷阱用例与本地文件联合。

用法::

    python -m demo.run_integrated_demo              # 两段都跑
    python -m demo.run_integrated_demo --offline    # 只跑 A
    python -m demo.run_integrated_demo --cases <path/to/cases.json>

环境变量 ``RAG_MCP_*`` 可覆盖默认路径；未设置时按仓库相对位置推断。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAG_ROOT = PROJECT_ROOT.parent / "evidence-reflective-trace-rag"
RAG_ROOT = Path(os.environ.get("RAG_MCP_PROJECT") or DEFAULT_RAG_ROOT)

# 必须在 import config 之前注入，配置在导入期完成校验
os.environ.setdefault("RAG_MCP_ENABLED", "true")
os.environ.setdefault("RAG_MCP_PYTHON", str(RAG_ROOT / ".venv" / "Scripts" / "python.exe"))
os.environ.setdefault("RAG_MCP_PROJECT", str(RAG_ROOT))
os.environ.setdefault("RAG_MCP_DATA_DIR", str(RAG_ROOT / "demo" / "data"))
os.environ.setdefault("RAG_MCP_DEFAULT_KB", "financial_report")
os.environ.setdefault("RAG_MCP_KB_ALIASES", "financial_report=财务报告,contract=合同条款")

import config
import knowledge_backend
import tools
from agent import run_agent
from demo.memory_budget import MemoryBudget
from mcp_client import RagMcpClient

SESSION_ID = "integrated-demo"
MAX_STEPS = 6
TASK_TIMEOUT = 120.0

RESULTS_REL = Path("experiment/results.md")
RESULTS_CONTENT = "验证集准确率为 86%，交付物为模型权重与评测报告。"
HYBRID_TASK = (
    "读取 experiment/results.md，并结合合同知识库判断："
    "实验结果中的交付物是否有合同依据？请分别说明本地材料与知识库证据。"
)

#: B 段按顺序执行的用例 id（agent_task 缺省时用 question）
AGENT_CASE_IDS = (
    "finance_revenue",
    "contract_termination",
    "contract_penalty_ratio",
    "contract_renewal_trap",
)

_CITATION_RE = re.compile(r"\[S\d+\]")
_EVIDENCE_WORDS = ("证据", "无法", "未", "不能")


# ---------------------------------------------------------------- 数据加载


@dataclass(frozen=True)
class Case:
    """cases.json 中的一条用例。"""

    id: str
    knowledge_base: str
    question: str
    expected_route: str
    expected_answer: str | None
    expected_citations: list[str]
    expected_failure_type: str | None = None
    agent_task: str | None = None
    note: str | None = None


def load_cases(path: Path | None = None) -> list[Case]:
    """读取两端共用的 cases.json。

    Raises:
        SystemExit: 文件不存在或结构不合法，附带修复提示
    """
    source = path or (RAG_ROOT / "demo" / "cases.json")
    if not source.exists():
        raise SystemExit(
            f"找不到两端共用的用例文件：{source}\n"
            "请在 RAG 仓库确认 demo/cases.json 存在，或用 --cases 指定路径。"
        )
    payload = json.loads(source.read_text(encoding="utf-8"))
    return [
        Case(
            id=item["id"],
            knowledge_base=item["knowledge_base"],
            question=item["question"],
            expected_route=item["expected_route"],
            expected_answer=item.get("expected_answer"),
            expected_citations=list(item.get("expected_citations") or []),
            expected_failure_type=item.get("expected_failure_type"),
            agent_task=item.get("agent_task"),
            note=item.get("note"),
        )
        for item in payload["cases"]
    ]


# ---------------------------------------------------------------- 结果结构


@dataclass
class Check:
    """一条联调断言。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EventLog:
    """收集 Agent 事件；知识库结果直接取 ``knowledge_result`` 事件。

    不解析 observation 文本：非 success 状态会在其后追加 ``[系统约束]``。
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    knowledge: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, event: str, data: dict[str, Any]) -> None:
        self.entries.append({"event": event, **data})
        if event == "knowledge_result":
            self.knowledge.append(data)


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _strip_citations(text: str | None) -> str:
    return _CITATION_RE.sub("", text or "").strip()


def _client() -> RagMcpClient:
    if config.RAG_MCP_PYTHON is None or config.RAG_MCP_PROJECT is None:
        raise SystemExit("RAG_MCP_PYTHON / RAG_MCP_PROJECT 未配置")
    if config.RAG_MCP_DATA_DIR is None:
        raise SystemExit("RAG_MCP_DATA_DIR 未配置")
    return RagMcpClient(
        python=config.RAG_MCP_PYTHON,
        project=config.RAG_MCP_PROJECT,
        data_dir=config.RAG_MCP_DATA_DIR,
        kb_aliases=config.RAG_MCP_KB_ALIASES,
        default_kb=config.RAG_MCP_DEFAULT_KB,
        startup_timeout=config.RAG_MCP_STARTUP_TIMEOUT,
    )


# ---------------------------------------------------------------- A. 链路自检


async def probe_contract(client: RagMcpClient, cases: list[Case]) -> list[Check]:
    """逐条调 knowledge_search，核对状态、答案事实与引用装配。"""
    checks: list[Check] = []

    health = await client.call_tool("rag_health", {}, timeout=30)
    ready = [kb["name"] for kb in health.get("knowledge_bases", []) if kb.get("ready")]
    checks.append(
        Check(
            "rag_health 报告知识库就绪",
            len(ready) >= 2,
            f"data_dir={health.get('data_dir')} ready={ready}",
        )
    )

    first_run_id: str | None = None
    for case in cases:
        payload = await client.call_tool(
            "knowledge_search",
            {"question": case.question, "knowledge_base": case.knowledge_base},
            timeout=config.RAG_MCP_TOOL_TIMEOUT,
        )
        status = payload.get("status")
        detail = f"status={status} citations={payload.get('citations')}"

        if case.expected_route == "success":
            answer = payload.get("answer") or ""
            ok = (
                status == "success"
                and _strip_citations(case.expected_answer) in _strip_citations(answer)
                and bool(payload.get("citations"))
                and all(cid in answer for cid in payload.get("citations") or [])
                and bool(payload.get("evidence"))
            )
            detail += f" answer={answer!r}"
            first_run_id = first_run_id or payload.get("run_id")
        else:
            ok = (
                status == case.expected_route
                and payload.get("answer") is None
                and payload.get("failure_type") == case.expected_failure_type
            )
            detail += f" failure_type={payload.get('failure_type')}"

        checks.append(Check(f"[{case.id}] {case.question}", ok, detail))

    if first_run_id:
        detail = await client.call_tool("rag_run_detail", {"run_id": first_run_id}, timeout=30)
        stages = [trace["stage"] for trace in detail.get("traces", [])]
        checks.append(
            Check(
                "run_id 可回查完整 Trace",
                "answer_verification" in stages,
                f"stages={stages}",
            )
        )
    return checks


# ---------------------------------------------------------------- B. 端到端


def _seed_workspace(workspace: Path) -> None:
    path = workspace / RESULTS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RESULTS_CONTENT, encoding="utf-8")


async def run_task(task: str, workspace: Path) -> tuple[str, EventLog]:
    """在一个临时工作区里跑一次完整任务。"""
    events = EventLog()
    previous = getattr(tools, "WORK_SPACE", None)
    tools.WORK_SPACE = workspace
    try:
        answer = await asyncio.wait_for(
            run_agent(
                task,
                SESSION_ID,
                max_steps=MAX_STEPS,
                budget=MemoryBudget(SESSION_ID, MAX_STEPS),
                on_event=events,
            ),
            timeout=TASK_TIMEOUT,
        )
    finally:
        if previous is not None:
            tools.WORK_SPACE = previous
    return answer, events


def _case_checks(case: Case, answer: str, events: EventLog, index: int) -> list[Check]:
    """按用例预期生成断言：success 要引用，abstain 不许编造，陷阱要看否定表述。"""
    checks: list[Check] = []
    statuses = [item.get("status") for item in events.knowledge]
    used = [item.get("knowledge_base") for item in events.knowledge]
    label = f"任务{index}"

    checks.append(
        Check(
            f"{label} 命中了预期知识库与状态",
            case.knowledge_base in used
            and any(status == case.expected_route for status in statuses),
            f"knowledge_base={used} status={statuses}",
        )
    )

    if case.expected_route == "success":
        checks.append(
            Check(
                f"{label} 最终答案保留引用",
                bool(_CITATION_RE.search(answer)),
                f"answer={answer!r}",
            )
        )
    else:
        checks.append(
            Check(
                f"{label} 拒答后未编造引用或具体数值",
                not _CITATION_RE.search(answer) and not re.search(r"\d+(?:\.\d+)?\s*%", answer),
                f"answer={answer!r}",
            )
        )
        checks.append(
            Check(
                f"{label} 明确说明证据不足",
                any(word in answer for word in _EVIDENCE_WORDS),
                f"answer={answer!r}",
            )
        )

    if case.id == "contract_renewal_trap":
        checks.append(
            Check(
                "陷阱用例按否定事实表述（未约定，而非约定）",
                "未约定" in answer or "没有约定" in answer,
                f"answer={answer!r}",
            )
        )
    return checks


async def run_agent_scenarios(cases: list[Case]) -> list[Check]:
    """由 ReAct 自主编排，验证引用保留、拒答不编造与本地文件联合。"""
    by_id = {case.id: case for case in cases}
    missing = [cid for cid in AGENT_CASE_IDS if cid not in by_id]
    if missing:
        raise SystemExit(f"cases.json 缺少联调所需用例：{missing}")

    checks: list[Check] = []
    with tempfile.TemporaryDirectory(prefix="integrated-demo-") as raw:
        workspace = Path(raw).resolve()
        _seed_workspace(workspace)

        for index, case_id in enumerate(AGENT_CASE_IDS, start=1):
            case = by_id[case_id]
            task = case.agent_task or case.question
            answer, events = await run_task(task, workspace)
            print(f"\n[任务{index}] {task}\n回答：{answer}")
            checks.extend(_case_checks(case, answer, events, index))

        answer, events = await run_task(HYBRID_TASK, workspace)
        print(f"\n[任务5] {HYBRID_TASK}\n回答：{answer}")
        read_calls = [
            entry
            for entry in events.entries
            if entry.get("event") == "tool_call" and entry.get("tool_name") == "read_file"
        ]
        checks.append(
            Check(
                "任务5 同时用了 read_file 与 knowledge_search",
                bool(read_calls) and bool(events.knowledge),
                f"read_file={len(read_calls)} knowledge={len(events.knowledge)}",
            )
        )
        checks.append(
            Check(
                "任务5 未把本地材料当作知识库证据",
                "[S1]" not in answer or bool(events.knowledge),
                f"answer={answer!r}",
            )
        )
    return checks


# ---------------------------------------------------------------- 入口


async def main(offline: bool, cases_path: Path | None) -> int:
    _ensure_utf8_stdout()
    cases = load_cases(cases_path)
    print(f"RAG 项目：{config.RAG_MCP_PROJECT}")
    print(f"RAG 数据：{config.RAG_MCP_DATA_DIR}")
    print(f"用例数据：{cases_path or (RAG_ROOT / 'demo' / 'cases.json')}（{len(cases)} 条）")

    client = _client()
    checks: list[Check] = []
    try:
        await client.start()
        print(f"MCP 已连接，工具：{list(client.tools)}")
        knowledge_backend.set_backend(knowledge_backend.McpKnowledgeBackend(client))

        checks.extend(await probe_contract(client, cases))

        if offline or not os.getenv("DEEPSEEK_API_KEY"):
            if not offline:
                print("\n未检测到 DEEPSEEK_API_KEY，跳过端到端任务。", file=sys.stderr)
        else:
            checks.extend(await run_agent_scenarios(cases))
    finally:
        knowledge_backend.set_backend(None)
        await client.close()

    print("\n===== 联调结果 =====")
    failed = 0
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        failed += 0 if check.passed else 1
        print(f"[{mark}] {check.name}  {check.detail}")
    print(f"合计 {len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ReAct Agent + RAG MCP 联合联调")
    parser.add_argument("--offline", action="store_true", help="只跑 MCP 链路自检，不调用 LLM")
    parser.add_argument("--cases", type=Path, default=None, help="自定义 cases.json 路径")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.offline, args.cases)))

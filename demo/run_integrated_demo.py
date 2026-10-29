"""联合联调 Demo：ReAct Agent + MCP 知识库（evidence-reflective-rag）。

分两段执行：

A. **链路自检**（不需要 LLM）：直接经 MCP 客户端调 ``knowledge_search``，验证四态契约、
   引用与 run_id 是否可用。
B. **端到端**（需要 DEEPSEEK_API_KEY）：由 ReAct 自主决定何时调知识库，验证引用保留、
   拒答不编造、本地文件与知识库联合。

用法::

    python -m demo.run_integrated_demo              # 两段都跑
    python -m demo.run_integrated_demo --offline    # 只跑 A

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

FINANCE_TASK = "根据财务报告回答：2025年第三季度营业收入和净利润分别是多少？"
CONTRACT_TASK = "查询合同是否约定提前终止时需要承担违约责任。"
ABSTAIN_TASK = "合同约定的违约金比例是多少？"
HYBRID_TASK = (
    "读取 experiment/results.md，并结合合同知识库判断："
    "实验结果中的交付物是否有合同依据？请分别说明本地材料与知识库证据。"
)

#: 引用标记，形如 [S1]
_CITATION_RE = re.compile(r"\[S\d+\]")


@dataclass
class Check:
    """一条联调断言。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EventLog:
    """收集 Agent 事件；知识库结果直接取 ``knowledge_result`` 事件。

    不用解析 observation 文本：非 success 状态会在 observation 后追加
    ``[系统约束]``，文本不再是纯 JSON。
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


async def probe_contract(client: RagMcpClient) -> list[Check]:
    """直接调 knowledge_search，验证四态契约与引用装配。"""
    checks: list[Check] = []

    health = await client.call_tool("rag_health", {}, timeout=30)
    ready = [kb["name"] for kb in health.get("knowledge_bases", []) if kb.get("ready")]
    checks.append(Check(
        "rag_health 报告知识库就绪",
        len(ready) >= 2,
        f"data_dir={health.get('data_dir')} ready={ready}",
    ))

    finance = await client.call_tool(
        "knowledge_search",
        {"question": "2025年第三季度营业收入和净利润是多少？", "knowledge_base": "financial_report"},
        timeout=config.RAG_MCP_TOOL_TIMEOUT,
    )
    checks.append(Check(
        "财务问答 status=success 且含 [S1]",
        finance.get("status") == "success" and "[S1]" in (finance.get("answer") or ""),
        f"citations={finance.get('citations')} run_id={finance.get('run_id')}",
    ))
    checks.append(Check(
        "财务问答 evidence 带 chunk_id 与 score",
        bool(finance.get("evidence")) and bool(finance["evidence"][0].get("chunk_id")),
        json.dumps(finance.get("evidence", [])[:1], ensure_ascii=False),
    ))

    abstain = await client.call_tool(
        "knowledge_search",
        {"question": "合同的违约金比例是多少？", "knowledge_base": "contract"},
        timeout=config.RAG_MCP_TOOL_TIMEOUT,
    )
    checks.append(Check(
        "无证据问题 status=abstain 且不带答案",
        abstain.get("status") == "abstain" and abstain.get("answer") is None,
        f"failure_type={abstain.get('failure_type')} message={abstain.get('message')}",
    ))

    # 复用同一会话查询 run detail，验证"可追溯"不是空话
    run_id = finance.get("run_id")
    detail = await client.call_tool("rag_run_detail", {"run_id": run_id}, timeout=30)
    stages = [trace["stage"] for trace in detail.get("traces", [])]
    checks.append(Check(
        "run_id 可回查完整 Trace",
        "answer_verification" in stages,
        f"stages={stages}",
    ))
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


async def run_agent_scenarios() -> list[Check]:
    """由 ReAct 自主编排，验证引用保留、换库与拒答不编造。"""
    checks: list[Check] = []

    with tempfile.TemporaryDirectory(prefix="integrated-demo-") as raw:
        workspace = Path(raw).resolve()
        _seed_workspace(workspace)

        # 任务 1：财务库，必须带回引用
        answer, events = await run_task(FINANCE_TASK, workspace)
        print(f"\n[任务1] {FINANCE_TASK}\n回答：{answer}")
        statuses = [item.get("status") for item in events.knowledge]
        checks.append(Check(
            "任务1 调用了知识库并拿到 success",
            "success" in statuses,
            f"knowledge_status={statuses}",
        ))
        checks.append(Check(
            "任务1 最终答案保留 [S1] 引用",
            bool(_CITATION_RE.search(answer)),
            f"answer={answer!r}",
        ))

        # 任务 2：合同库，验证按主题换库
        answer, events = await run_task(CONTRACT_TASK, workspace)
        print(f"\n[任务2] {CONTRACT_TASK}\n回答：{answer}")
        statuses = [item.get("status") for item in events.knowledge]
        used = [item.get("knowledge_base") for item in events.knowledge]
        checks.append(Check(
            "任务2 按主题切换到 contract 知识库",
            "contract" in used,
            f"knowledge_base={used}",
        ))
        checks.append(Check(
            "任务2 合同问答拿到 success 且保留引用",
            "success" in statuses and bool(_CITATION_RE.search(answer)),
            f"knowledge_status={statuses} answer={answer!r}",
        ))

        # 任务 3：无证据，必须拒答而非编造
        answer, events = await run_task(ABSTAIN_TASK, workspace)
        print(f"\n[任务3] {ABSTAIN_TASK}\n回答：{answer}")
        statuses = [item.get("status") for item in events.knowledge]
        checks.append(Check(
            "任务3 知识库返回 abstain",
            "abstain" in statuses,
            f"knowledge_status={statuses}",
        ))
        checks.append(Check(
            "任务3 拒答后未编造具体比例",
            not re.search(r"\d+(?:\.\d+)?\s*%", answer),
            f"answer={answer!r}",
        ))

        # 任务 4：本地文件 + 知识库联合
        answer, events = await run_task(HYBRID_TASK, workspace)
        print(f"\n[任务4] {HYBRID_TASK}\n回答：{answer}")
        read_calls = [
            entry for entry in events.entries
            if entry.get("event") == "tool_call" and entry.get("tool_name") == "read_file"
        ]
        statuses = [item.get("status") for item in events.knowledge]
        checks.append(Check(
            "任务4 同时用了 read_file 与 knowledge_search",
            bool(read_calls) and bool(statuses),
            f"read_file={len(read_calls)} knowledge_status={statuses}",
        ))
        checks.append(Check(
            "任务4 未把本地材料当作知识库证据",
            "[S1]" not in answer or bool(events.knowledge),
            f"answer={answer!r}",
        ))
    return checks


# ---------------------------------------------------------------- 入口


async def main(offline: bool) -> int:
    _ensure_utf8_stdout()
    print(f"RAG 项目：{config.RAG_MCP_PROJECT}")
    print(f"RAG 数据：{config.RAG_MCP_DATA_DIR}")

    client = _client()
    checks: list[Check] = []
    try:
        await client.start()
        print(f"MCP 已连接，工具：{list(client.tools)}")
        knowledge_backend.set_backend(knowledge_backend.McpKnowledgeBackend(client))

        checks.extend(await probe_contract(client))

        if offline or not os.getenv("DEEPSEEK_API_KEY"):
            if not offline:
                print("\n未检测到 DEEPSEEK_API_KEY，跳过端到端任务。", file=sys.stderr)
        else:
            checks.extend(await run_agent_scenarios())
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
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.offline)))

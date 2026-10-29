"""Run a complete deterministic Agent flow in a temporary workspace."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tools
from agent import run_agent
from demo.memory_budget import MemoryBudget
from demo.scripted_llm import ScriptedLLM

SESSION_ID = "demo-session"
MAX_STEPS = 6
TIMEOUT_SECONDS = 30.0

TASK = "读取 experiment/config.json 和 experiment/results.md，并将结论写入 experiment/summary.md"

CONFIG_REL = Path("experiment/config.json")
RESULTS_REL = Path("experiment/results.md")
SUMMARY_REL = Path("experiment/summary.md")

CONFIG_CONTENT = '{"model":"baseline","epochs":10}'
RESULTS_CONTENT = "验证集准确率为 86%。"

_MISSING = object()
_WORKSPACE_LOCK = asyncio.Lock()


@dataclass
class EventLog:
    """Collect agent events; observer failures never break the agent loop."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, event: str, data: dict[str, Any]) -> None:
        seq = len(self.entries) + 1
        try:
            self.entries.append({"seq": seq, "event": event, **data})
        except Exception as exc:  # observer must be best-effort
            self.entries.append(
                {
                    "seq": seq,
                    "event": "observer_error",
                    "original_event": event,
                    "error": repr(exc),
                }
            )


@contextmanager
def workspace_override(workspace: Path) -> Iterator[None]:
    """Temporarily point tools.WORK_SPACE at a demo workspace, restoring exactly."""
    previous = getattr(tools, "WORK_SPACE", _MISSING)
    tools.WORK_SPACE = workspace
    try:
        yield
    finally:
        if previous is _MISSING:
            try:
                delattr(tools, "WORK_SPACE")
            except AttributeError:
                pass
        else:
            tools.WORK_SPACE = previous


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _seed_workspace(workspace: Path) -> None:
    for rel, content in ((CONFIG_REL, CONFIG_CONTENT), (RESULTS_REL, RESULTS_CONTENT)):
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _dump_events(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        print(json.dumps(entry, ensure_ascii=False, default=str))


async def _run_demo(workspace: Path, events: EventLog) -> str:
    return await asyncio.wait_for(
        run_agent(
            TASK,
            SESSION_ID,
            max_steps=MAX_STEPS,
            llm_call=ScriptedLLM(TASK),
            budget=MemoryBudget(SESSION_ID, MAX_STEPS),
            on_event=events,
        ),
        timeout=TIMEOUT_SECONDS,
    )


async def main() -> int:
    _ensure_utf8_stdout()
    events = EventLog()

    with tempfile.TemporaryDirectory(prefix="text-react-demo-") as raw:
        workspace = Path(raw).resolve()
        _seed_workspace(workspace)

        async with _WORKSPACE_LOCK:
            with workspace_override(workspace):
                try:
                    answer = await _run_demo(workspace, events)
                except TimeoutError:
                    print(f"Agent 执行超时（>{TIMEOUT_SECONDS:.0f}s）。", file=sys.stderr)
                    print("事件列表：")
                    _dump_events(events.entries)
                    return 2
                except Exception as exc:  # demo should report, not crash
                    print(f"Agent 执行失败：{exc!r}", file=sys.stderr)
                    print("事件列表：")
                    _dump_events(events.entries)
                    return 1

        print("最终答案：", answer)
        print("事件列表：")
        _dump_events(events.entries)

        summary_path = workspace / SUMMARY_REL
        print(f"{SUMMARY_REL}：")
        if not summary_path.exists():
            print("（未生成）", file=sys.stderr)
            print("工作区临时目录将在 Demo 结束后清理。")
            return 3
        print(summary_path.read_text(encoding="utf-8"))
        print("工作区临时目录将在 Demo 结束后清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

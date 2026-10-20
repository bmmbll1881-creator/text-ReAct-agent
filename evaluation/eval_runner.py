"""运行隔离的离线评估 / Run deterministic, isolated offline evaluations."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tools
from agent import call_llm
from agent import run_agent as _run_agent
from demo.memory_budget import MemoryBudget
from demo.scripted_llm import ScriptedLLM

run_agent = _run_agent


async def offline_run_agent(prompt: str, session_id: str, **kwargs: Any) -> str:
    """执行最小本地文件动作 / Execute deterministic local file actions without LLM or Redis."""
    lowered = prompt.lower()
    outputs = []
    if "hello.txt" in lowered:
        try:
            outputs.append((tools.WORK_SPACE / "hello.txt").read_text(encoding="utf-8"))
        except FileNotFoundError:
            outputs.append("文件不存在")
    if "data.txt" in lowered:
        try:
            outputs.append((tools.WORK_SPACE / "data.txt").read_text(encoding="utf-8"))
        except FileNotFoundError:
            outputs.append("文件不存在")
    if "note.txt" in lowered:
        target = tools.WORK_SPACE / "note.txt"
        if "追加" in prompt:
            with target.open("a", encoding="utf-8") as file:
                file.write("world")
            outputs.append("hello world note.txt")
        else:
            target.write_text("hello", encoding="utf-8")
            outputs.append("hello note.txt")
    if "result.txt" in lowered:
        (tools.WORK_SPACE / "result.txt").write_text("42", encoding="utf-8")
        outputs.append("42 result.txt")
    if "missing.txt" in lowered:
        outputs.append("文件不存在")
    return " ".join(outputs) or prompt


@contextmanager
def isolated_workspace(path: Path):
    old_env = os.environ.get("WORKSPACE")
    old_legacy = os.environ.get("WORK_SPACE")
    old_workspace = tools.WORK_SPACE
    os.environ["WORKSPACE"] = str(path)
    os.environ["WORK_SPACE"] = str(path)
    tools.WORK_SPACE = path.resolve()
    try:
        yield
    finally:
        tools.WORK_SPACE = old_workspace
        env = os.environ
        if old_env is None: env.pop("WORKSPACE", None)
        else: env["WORKSPACE"] = old_env
        if old_legacy is None: env.pop("WORK_SPACE", None)
        else: env["WORK_SPACE"] = old_legacy


def check_expected(output: str, expected: Any) -> bool:
    if isinstance(expected, list):
        return all(str(item).lower() in output.lower() for item in expected)
    if isinstance(expected, dict):
        all_items = expected.get("all", [])
        any_items = expected.get("any", [])
        regex_items = expected.get("regex", [])
        if not all(str(item).lower() in output.lower() for item in all_items):
            return False
        if any_items and not any(str(item).lower() in output.lower() for item in any_items):
            return False
        return all(re.search(pattern, output, re.IGNORECASE) for pattern in regex_items)
    return False


def _write_setup(workspace: Path, setup: Iterable[dict[str, str]]) -> None:
    for item in setup:
        target = (workspace / item["file"]).resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise ValueError("setup file escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.get("content", ""), encoding="utf-8")


async def evaluate(tasks: list[dict[str, Any]], model: str | None = None, *, offline: bool = False, deterministic_agent: bool = False, real_llm: bool = False) -> list[dict[str, Any]]:
    results = []
    for task in tasks:
        started = time.perf_counter()
        output = ""
        status = "failed"
        error = None
        events: list[tuple[str, dict[str, Any]]] = []
        with tempfile.TemporaryDirectory(prefix="agent-eval-") as raw:
            workspace = Path(raw)
            _write_setup(workspace, task.get("setup", []))
            session_id = f"eval-{task['name']}-{uuid.uuid4().hex}"
            try:
                kwargs = {}
                async def collect_event(name: str, payload: dict[str, Any], event_log=events) -> None:
                    event_log.append((name, payload))

                kwargs["on_event"] = collect_event
                if model:
                    kwargs["llm_call"] = call_llm
                if task.get("max_steps") and not deterministic_agent:
                    kwargs["max_steps"] = task["max_steps"]
                if deterministic_agent:
                    max_steps = task.get("max_steps", 12)
                    kwargs.update(llm_call=ScriptedLLM(task["prompt"], task.get("replies")),
                                  budget=MemoryBudget(session_id, max_steps), max_steps=max_steps)
                with isolated_workspace(workspace):
                    runner = offline_run_agent if offline else run_agent
                    try:
                        result = await runner(task["prompt"], session_id, **kwargs)
                        output = str(result)
                    except Exception as exc:
                        if not task.get("expected_error") or (isinstance(task["expected_error"], str) and task["expected_error"].lower() not in str(exc).lower()):
                            raise
                        error = str(exc)
                event_output = "\n".join(
                    f"{name}: {json.dumps(payload, ensure_ascii=False, default=str)}"
                    for name, payload in events
                )
                file_output = "\n".join(
                    f"{path.relative_to(workspace).as_posix()}: {path.read_text(encoding='utf-8')}"
                    for path in sorted(workspace.rglob("*"))
                    if path.is_file()
                )
                output = "\n".join(part for part in (output, event_output, file_output) if part)
                for expected_file in task.get("expected_files", []):
                    if not (workspace / expected_file).is_file():
                        raise AssertionError(f"expected output file was not created: {expected_file}")
                for unexpected_file in task.get("absent_files", []):
                    if (workspace / unexpected_file).exists():
                        raise AssertionError(f"unexpected file was created: {unexpected_file}")
                if task.get("expected_error") and not real_llm and not error:
                    raise AssertionError(f"expected execution error: {task['expected_error']}")
                expected_events = [] if real_llm else task.get("expected_events", [])
                missing_events = set(expected_events) - {name for name, _ in events}
                if missing_events:
                    raise AssertionError(f"expected events were not emitted: {sorted(missing_events)}")
                if task.get("expected_error") and error:
                    output = f"{output}\n{error}"
                expected = task.get("online_expected", task.get("expected")) if real_llm else task.get("expected")
                status = "passed" if check_expected(output, expected) else "failed"
            except Exception as exc:  # 记录单项失败而不中断评估 / Record task failure without aborting the suite
                error = str(exc)
            steps = sum(1 for name, _ in events if name == "step_start")
            results.append({"name": task["name"], "category": task.get("category", "uncategorized"), "status": status, "passed": status == "passed", "steps": steps, "output": output, "error": error, "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
    return results


async def main_async(args: argparse.Namespace) -> int:
    tasks = json.loads(Path(args.tasks_file).read_text(encoding="utf-8"))
    selected = set(filter(None, (args.tasks or "").split(",")))
    if selected:
        tasks = [task for task in tasks if task["name"] in selected]
        if not tasks:
            raise ValueError(f"no evaluation tasks matched: {sorted(selected)}")
    # Tests and embeddings can inject ``run_agent``; CLI defaults to deterministic offline mode.
    offline = getattr(args, "offline", None)
    if offline is None:
        offline = not getattr(args, "online", False) and run_agent is _run_agent
    deterministic_agent = getattr(args, "deterministic_agent", False)
    if deterministic_agent:
        offline = False
    results = await evaluate(tasks, args.model, offline=offline,
                             deterministic_agent=deterministic_agent,
                             real_llm=getattr(args, "online", False))
    passed = sum(item["passed"] for item in results)
    total = len(results)
    categories = {}
    for result in results:
        group = categories.setdefault(result["category"], {"total": 0, "passed": 0, "pass_rate": 0.0})
        group["total"] += 1
        group["passed"] += int(result["passed"])
    for group in categories.values():
        group["pass_rate"] = group["passed"] / group["total"] if group["total"] else 0.0
    report = {"summary": {"total": total, "passed": passed, "failed": total - passed, "pass_rate": passed / total if total else 0.0, "average_steps": sum(item["steps"] for item in results) / total if total else 0.0, "average_duration_ms": sum(item["duration_ms"] for item in results) / total if total else 0.0, "categories": categories, "fail_under": args.fail_under}, "results": results}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_path:
        path = Path(args.report_path); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 1 if report["summary"]["pass_rate"] < args.fail_under else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", help="comma-separated task names")
    parser.add_argument("--tasks-file", default=str(Path(__file__).with_name("eval_tasks.json")))
    parser.add_argument("--report-path", default="evaluation/report.json")
    parser.add_argument("--fail-under", type=float, default=0.8)
    parser.add_argument("--model")
    parser.add_argument("--online", action="store_true", help="use real LLM/Redis agent")
    parser.add_argument("--deterministic-agent", action="store_true",
                        help="run real Agent loop with ScriptedLLM and in-memory budget")
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()

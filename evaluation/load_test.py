"""Process-isolated load test for the deterministic Agent main loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import tempfile
import time
from pathlib import Path


def _run_one(index: int) -> float:
    logging.disable(logging.CRITICAL)
    import tools
    from agent import run_agent
    from demo.memory_budget import MemoryBudget
    from demo.scripted_llm import ScriptedLLM

    with tempfile.TemporaryDirectory(prefix="agent-load-") as raw:
        workspace = Path(raw).resolve()
        tools.WORK_SPACE = workspace
        session_id = f"load-{index}"
        replies = [
            'Thought: write output\nAction: write_file\nAction Input: {"path":"result.txt","content":"ok","mode":"w"}',
            "Final Answer: done",
        ]
        started = time.perf_counter()
        asyncio.run(
            run_agent(
                "create result.txt",
                session_id,
                llm_call=ScriptedLLM(replies=replies),
                budget=MemoryBudget(session_id, 2),
                max_steps=2,
            )
        )
        elapsed = (time.perf_counter() - started) * 1000
        if (workspace / "result.txt").read_text(encoding="utf-8") != "ok":
            raise AssertionError("load task produced incorrect output")
        return elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", default="evaluation/load-report.json")
    args = parser.parse_args()
    if args.requests < 1 or args.workers < 1:
        parser.error("requests and workers must be positive")

    from concurrent.futures import ProcessPoolExecutor

    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        durations = list(pool.map(_run_one, range(args.requests)))
    wall_seconds = time.perf_counter() - started
    ordered = sorted(durations)
    report = {
        "mode": "deterministic_agent_in_memory_budget",
        "requests": args.requests,
        "workers": args.workers,
        "completed": len(durations),
        "wall_seconds": round(wall_seconds, 4),
        "throughput_requests_per_second": round(len(durations) / wall_seconds, 2),
        "latency_ms": {
            "average": round(statistics.mean(durations), 3),
            "p50": round(statistics.median(durations), 3),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            "max": round(max(durations), 3),
        },
        "note": "Does not measure network LLM or Redis latency/contention.",
    }
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

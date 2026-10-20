"""Verify shared Redis budgets and locks across independent processes."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import time
import uuid


def _consume_worker(session_id: str, count: int, output) -> None:
    from budget import RedisBudget

    budget = RedisBudget(session_id, count * 4)
    try:
        output.put((True, [budget.consume() for _ in range(count)]))
    except Exception as error:
        output.put((False, str(error)))


def _lock_worker(session_id: str, output) -> None:
    from budget import StepBudget

    budget = StepBudget(session_id, 2)
    try:
        budget.acquire_lock(blocking_timeout=10)
        output.put((True, "acquired"))
        budget.release_lock()
    except Exception as error:
        output.put((False, str(error)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=int, default=2)
    parser.add_argument("--operations", type=int, default=10)
    args = parser.parse_args()
    if args.instances < 2 or args.operations < 1:
        parser.error("requires at least two instances and one operation")

    import redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), socket_connect_timeout=3)
    try:
        client.ping()
    except redis.RedisError as error:
        print({"status": "blocked", "reason": type(error).__name__})
        return 2
    suffix = uuid.uuid4().hex
    session_id = f"acceptance-{suffix}"
    budget_key = f"agent:budget:{session_id}"
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    started = time.perf_counter()
    processes = [context.Process(target=_consume_worker, args=(session_id, args.operations, output))
                 for _ in range(args.instances)]
    try:
        for process in processes:
            process.start()
        responses = [output.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
        if any(process.exitcode != 0 for process in processes) or not all(ok for ok, _ in responses):
            raise RuntimeError(f"budget worker failure: {responses}")
        expected = args.instances * args.operations
        actual = int(client.get(budget_key) or 0)
        if actual != expected:
            raise AssertionError(f"shared budget count {actual} != {expected}")

        budget_seconds = time.perf_counter() - started
        lock_session = f"acceptance-lock-{suffix}"
        lock_results = context.Queue()
        lock_processes = [context.Process(target=_lock_worker, args=(lock_session, lock_results))
                          for _ in range(args.instances)]
        for process in lock_processes:
            process.start()
        outcomes = [lock_results.get(timeout=30) for _ in lock_processes]
        for process in lock_processes:
            process.join(timeout=30)
        if any(process.exitcode != 0 for process in lock_processes) or not all(ok for ok, _ in outcomes):
            raise RuntimeError(f"lock worker failure: {outcomes}")
        print({"status": "passed", "instances": args.instances, "operations": expected,
               "shared_budget_count": actual, "lock_acquisitions": len(outcomes),
               "budget_duration_seconds": round(budget_seconds, 4),
               "budget_operations_per_second": round(expected / budget_seconds, 2)})
        return 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        client.delete(budget_key, f"agent:history:{session_id}",
                      f"react-agent:lock:acceptance-lock-{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())

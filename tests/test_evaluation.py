import argparse
import json
from pathlib import Path

import pytest

from evaluation import eval_runner


@pytest.mark.parametrize(
    ("output", "expected", "passed"),
    [
        ("Hello World", ["hello", "world"], True),
        ("Found WORLD", {"any": ["missing", "world"]}, True),
        ("item-42", {"regex": [r"item-\d+"]}, True),
        ("hello", {"all": []}, True),
        ("hello", {"all": ["world"]}, False),
        ("hello", {}, True),
    ],
)
def test_check_expected(output, expected, passed):
    assert eval_runner.check_expected(output, expected) is passed


@pytest.mark.asyncio
async def test_evaluate_isolates_workspaces(monkeypatch):
    seen = []

    async def fake_run_agent(prompt, session_id, **kwargs):
        workspace = Path(eval_runner.tools.WORK_SPACE)
        seen.append((workspace, (workspace / "from_a.txt").exists()))
        if prompt == "a":
            (workspace / "from_a.txt").write_text("a", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(eval_runner, "run_agent", fake_run_agent)
    results = await eval_runner.evaluate([
        {"name": "a", "prompt": "a", "setup": [], "expected": {"all": ["ok"]}},
        {"name": "b", "prompt": "b", "setup": [], "expected": {"all": ["ok"]}},
    ])
    assert [item["passed"] for item in results] == [True, True]
    assert seen[0][0] != seen[1][0]
    assert seen[1][1] is False


@pytest.mark.asyncio
async def test_main_async_subset_report_and_threshold(monkeypatch, tmp_path):
    tasks_file = tmp_path / "tasks.json"
    report_path = tmp_path / "report.json"
    tasks_file.write_text(json.dumps([
        {"name": "pass", "prompt": "p", "setup": [], "expected": {"all": ["ok"]}},
        {"name": "fail", "prompt": "f", "setup": [], "expected": {"all": ["no"]}},
    ]), encoding="utf-8")

    async def fake_run_agent(prompt, session_id, **kwargs):
        return "ok"

    monkeypatch.setattr(eval_runner, "run_agent", fake_run_agent)
    args = argparse.Namespace(tasks="pass", tasks_file=str(tasks_file), report_path=str(report_path), fail_under=1.0, model=None)
    assert await eval_runner.main_async(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"] == {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1.0, "fail_under": 1.0}
    args.tasks = None
    args.fail_under = 0.75
    assert await eval_runner.main_async(args) == 1

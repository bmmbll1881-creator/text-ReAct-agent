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
    results = await eval_runner.evaluate(
        [
            {"name": "a", "prompt": "a", "setup": [], "expected": {"all": ["ok"]}},
            {"name": "b", "prompt": "b", "setup": [], "expected": {"all": ["ok"]}},
        ]
    )
    assert [item["passed"] for item in results] == [True, True]
    assert seen[0][0] != seen[1][0]
    assert seen[1][1] is False


@pytest.mark.asyncio
async def test_evaluate_checks_events_and_generated_files(monkeypatch):
    async def fake_run_agent(prompt, session_id, **kwargs):
        workspace = Path(eval_runner.tools.WORK_SPACE)
        (workspace / "summary.md").write_text("accuracy 86%", encoding="utf-8")
        await kwargs["on_event"]("final_answer", {"answer": "done"})
        return "done"

    monkeypatch.setattr(eval_runner, "run_agent", fake_run_agent)
    result = await eval_runner.evaluate(
        [
            {
                "name": "summary",
                "prompt": "summarize",
                "setup": [],
                "expected_files": ["summary.md"],
                "expected_events": ["final_answer"],
                "expected": {"all": ["done", "86%"]},
            }
        ]
    )
    assert result[0]["passed"] is True


@pytest.mark.asyncio
async def test_main_async_rejects_unknown_task_selection(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps([{"name": "known", "prompt": "p"}]), encoding="utf-8")
    args = argparse.Namespace(
        tasks="missing",
        tasks_file=str(tasks_file),
        report_path=None,
        fail_under=0.8,
        model=None,
        online=False,
        deterministic_agent=False,
    )
    with pytest.raises(ValueError, match="no evaluation tasks matched"):
        await eval_runner.main_async(args)


@pytest.mark.asyncio
async def test_main_async_subset_report_and_threshold(monkeypatch, tmp_path):
    tasks_file = tmp_path / "tasks.json"
    report_path = tmp_path / "report.json"
    tasks_file.write_text(
        json.dumps(
            [
                {"name": "pass", "prompt": "p", "setup": [], "expected": {"all": ["ok"]}},
                {"name": "fail", "prompt": "f", "setup": [], "expected": {"all": ["no"]}},
            ]
        ),
        encoding="utf-8",
    )

    async def fake_run_agent(prompt, session_id, **kwargs):
        return "ok"

    monkeypatch.setattr(eval_runner, "run_agent", fake_run_agent)
    args = argparse.Namespace(
        tasks="pass",
        tasks_file=str(tasks_file),
        report_path=str(report_path),
        fail_under=1.0,
        model=None,
    )
    assert await eval_runner.main_async(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["total"] == 1
    assert report["summary"]["passed"] == 1
    assert report["summary"]["pass_rate"] == 1.0
    assert report["summary"]["categories"]["uncategorized"]["pass_rate"] == 1.0
    args.tasks = None
    args.fail_under = 0.75
    assert await eval_runner.main_async(args) == 1


def test_deterministic_suite_covers_required_categories():
    tasks = json.loads(Path("evaluation/deterministic_tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 40
    assert {task["category"] for task in tasks} == {
        "multi_step",
        "error_recovery",
        "security",
        "budget",
    }


@pytest.mark.asyncio
async def test_evaluate_accepts_any_expected_error(monkeypatch):
    async def failing_run_agent(prompt, session_id, **kwargs):
        raise RuntimeError("expected failure")

    monkeypatch.setattr(eval_runner, "run_agent", failing_run_agent)
    result = await eval_runner.evaluate(
        [
            {
                "name": "failure",
                "prompt": "p",
                "setup": [],
                "expected_error": True,
                "expected": {"all": []},
            }
        ]
    )
    assert result[0]["passed"] is True


@pytest.mark.asyncio
async def test_real_llm_evaluation_skips_scripted_event_contract(monkeypatch):
    async def final_answer_only(prompt, session_id, **kwargs):
        return "ok"

    monkeypatch.setattr(eval_runner, "run_agent", final_answer_only)
    result = await eval_runner.evaluate(
        [
            {
                "name": "online",
                "prompt": "p",
                "setup": [],
                "expected_events": ["tool_call"],
                "expected": {"all": ["ok"]},
            }
        ],
        real_llm=True,
    )
    assert result[0]["passed"] is True


def test_online_suite_covers_non_budget_scenarios():
    tasks = json.loads(Path("evaluation/online_tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 31
    assert all(task["category"] != "budget" for task in tasks)
    assert all("online_expected" in task for task in tasks if task["category"] == "security")

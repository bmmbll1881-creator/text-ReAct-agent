import pytest
from pydantic import ValidationError

from config import MAX_READ_CHARS, MAX_WRITE_CHARS
from tools import (
    ReadFileInput,
    WriteFileInput,
    execute_tool,
    execute_tool_from_dict,
)


def test_writes_reads_and_appends_in_workspace(workspace):
    execute_tool_from_dict("write_file", {"path": "nested/note.txt", "content": "first"})
    execute_tool_from_dict("write_file", {"path": "nested/note.txt", "content": " second", "mode": "a"})

    assert execute_tool_from_dict("read_file", {"path": "nested/note.txt"}) == "first second"
    assert (workspace / "nested" / "note.txt").exists()


@pytest.mark.parametrize("path", ["../outside.txt", "C:/outside.txt", "script.py"])
def test_rejects_unsafe_or_unsupported_path(workspace, path):
    with pytest.raises(ValueError):
        execute_tool_from_dict("write_file", {"path": path, "content": "no"})


def test_read_truncates_content_over_limit(workspace):
    execute_tool_from_dict("write_file", {"path": "large.txt", "content": "a" * (MAX_READ_CHARS + 1)})

    assert execute_tool_from_dict("read_file", {"path": "large.txt"}).startswith("a" * MAX_READ_CHARS)


def test_write_truncates_content_over_limit(workspace):
    execute_tool_from_dict("write_file", {"path": "large.txt", "content": "a" * (MAX_WRITE_CHARS + 1)})
    assert (workspace / "large.txt").read_text(encoding="utf-8") == "a" * MAX_WRITE_CHARS


@pytest.mark.parametrize("payload", [None, {}, {"path": 3}, {"path": "x.txt", "content": 3}])
def test_validates_input_types(workspace, payload):
    with pytest.raises(ValueError):
        execute_tool_from_dict("write_file", payload)


def test_rejects_unknown_tool():
    with pytest.raises(ValueError):
        execute_tool("delete_file", {})


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ReadFileInput, {"path": "note.txt", "unexpected": True}),
        (WriteFileInput, {"path": "note.txt", "content": "x", "unexpected": True}),
    ],
)
def test_models_reject_extra_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("path", ["/tmp/outside.txt", "../outside.txt", "note.exe"])
def test_registry_rejects_unsafe_and_non_whitelisted_paths(workspace, path):
    with pytest.raises(ValueError):
        execute_tool_from_dict("write_file", {"path": path, "content": "x"})


def test_read_cache_is_cleared_after_write(workspace):
    execute_tool_from_dict("write_file", {"path": "cached.txt", "content": "old"})
    assert execute_tool_from_dict("read_file", {"path": "cached.txt"}) == "old"

    (workspace / "cached.txt").write_text("external", encoding="utf-8")
    assert execute_tool_from_dict("read_file", {"path": "cached.txt"}) == "old"

    execute_tool_from_dict("write_file", {"path": "cached.txt", "content": "new"})
    assert execute_tool_from_dict("read_file", {"path": "cached.txt"}) == "new"


def test_registry_truncates_oversized_write_and_logs(workspace, capsys):
    result = execute_tool_from_dict(
        "write_file", {"path": "trimmed.txt", "content": "x" * (MAX_WRITE_CHARS + 1)}
    )

    assert "trimmed.txt" in result
    assert (workspace / "trimmed.txt").read_text(encoding="utf-8") == "x" * MAX_WRITE_CHARS
    assert '"event": "tool_content_truncated"' in capsys.readouterr().out

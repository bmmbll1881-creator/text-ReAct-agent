import pytest

from tools import MAX_READ_CHS, MAX_WRITE_CHS, execute_tool, read_file, write_file


def test_writes_reads_and_appends_in_workspace(workspace):
    write_file({"path": "nested/note.txt", "content": "first"})
    write_file({"path": "nested/note.txt", "content": " second", "mode": "a"})

    assert read_file({"path": "nested/note.txt"}) == "first second"
    assert (workspace / "nested" / "note.txt").exists()


@pytest.mark.parametrize("path", ["../outside.txt", "C:/outside.txt", "script.py"])
def test_rejects_unsafe_or_unsupported_path(workspace, path):
    with pytest.raises(ValueError):
        write_file({"path": path, "content": "no"})


def test_read_truncates_content_over_limit(workspace):
    write_file({"path": "large.txt", "content": "a" * (MAX_READ_CHS + 1)})

    assert read_file({"path": "large.txt"}).startswith("a" * MAX_READ_CHS)


def test_write_rejects_content_over_limit(workspace):
    with pytest.raises(ValueError):
        write_file({"path": "large.txt", "content": "a" * (MAX_WRITE_CHS + 1)})


@pytest.mark.parametrize("payload", [None, {}, {"path": 3}, {"path": "x.txt", "content": 3}])
def test_validates_input_types(workspace, payload):
    with pytest.raises(ValueError):
        write_file(payload)


def test_rejects_unknown_tool():
    with pytest.raises(ValueError):
        execute_tool("delete_file", {})

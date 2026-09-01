import pytest

from parser import parse_response


def test_parses_final_answer_and_thought():
    result = parse_response("Thought: done\nFinal Answer: created")

    assert result.thought == "done"
    assert result.final_answer == "created"


def test_parses_nested_json_and_braces_in_string():
    result = parse_response(
        'Thought: write\nAction: write_file\n'
        'Action Input: {"path": "note.txt", "content": "{hello}"}'
    )

    assert result.action == "write_file"
    assert result.action_input == {"path": "note.txt", "content": "{hello}"}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("Thought: x", "Action"),
        ("Action: read_file", "Input"),
        ("Action: read_file\nAction Input: {bad}", "Input"),
        ("Action: read_file\nAction Input: prefix {\"path\": \"x.txt\"}", "Input"),
        ("Action: read_file\nAction Input: {\"path\": \"x.txt\"} extra", "Input"),
    ],
)
def test_rejects_invalid_protocol(response, expected):
    assert expected in parse_response(response).error


def test_accepts_code_fences():
    result = parse_response("```\nAction: read_file\nAction Input: {\"path\": \"x.txt\"}\n```")

    assert result.action_input == {"path": "x.txt"}

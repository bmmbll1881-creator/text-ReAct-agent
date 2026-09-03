import json

from logger import log_event, set_sensitive_debug


def test_log_event_emits_one_json_object(capsys):
    log_event("tool_called", "session-1", step="read", attempt=2)

    entry = json.loads(capsys.readouterr().out)
    assert entry["event"] == "tool_called"
    assert entry["session_id"] == "session-1"
    assert entry["step"] == "read"
    assert entry["attempt"] == 2
    assert "timestamp" in entry


def test_log_event_redacts_sensitive_values(capsys):
    set_sensitive_debug(False)
    log_event("request", "session-1", api_key="secret", access_token="token", content="private")

    entry = json.loads(capsys.readouterr().out)
    assert entry["api_key"] == "[REDACTED]"
    assert entry["access_token"] == "[REDACTED]"
    assert "private" not in entry["content"]

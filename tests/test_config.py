import pytest

from config import Config


def test_from_env_reads_explicit_values(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_URL", "https://example.test/chat")
    monkeypatch.setenv("MAX_STEPS", "3")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))

    config = Config.from_env()

    assert config.chat_url == "https://example.test/chat"
    assert config.max_steps == 3
    assert config.workspace == tmp_path.resolve()


def test_from_env_uses_defaults(monkeypatch):
    monkeypatch.setenv("CHAT_URL", "https://example.test/chat")
    monkeypatch.delenv("MAX_STEPS", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    config = Config.from_env()

    assert config.max_steps == 10
    assert config.llm_model == "deepseek-chat"


def test_from_env_requires_chat_url(monkeypatch):
    monkeypatch.delenv("CHAT_URL", raising=False)

    with pytest.raises(ValueError, match="CHAT_URL"):
        Config.from_env()

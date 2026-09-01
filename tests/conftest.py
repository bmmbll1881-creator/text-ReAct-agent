from pathlib import Path

import pytest


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_SPACE", str(tmp_path))
    import tools

    monkeypatch.setattr(tools, "WORK_SPACE", Path(tmp_path).resolve())
    return tmp_path

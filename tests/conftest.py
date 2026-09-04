from pathlib import Path
import asyncio
import inspect

import pytest


def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests in environments without pytest-asyncio installed."""
    test_func = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_func):
        asyncio.run(test_func(**{name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}))
        return True
    return None


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_SPACE", str(tmp_path))
    import tools

    monkeypatch.setattr(tools, "WORK_SPACE", Path(tmp_path).resolve())
    return tmp_path

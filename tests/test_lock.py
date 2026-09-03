import pytest

from budget import SessionBusyError, StepBudget


class FakeLock:
    def __init__(self, available):
        self.available = available
        self.released = False

    def acquire(self, **_kwargs):
        return self.available

    def release(self):
        self.released = True


class FakeRedis:
    def __init__(self, available=True):
        self.lock_instance = FakeLock(available)

    def lock(self, *_args, **_kwargs):
        return self.lock_instance


def make_budget(monkeypatch, available=True):
    fake = FakeRedis(available)
    monkeypatch.setattr("budget.redis.from_url", lambda *_args, **_kwargs: fake)
    return StepBudget("locked-session", 2), fake


def test_acquire_and_release_lock(monkeypatch):
    budget, fake = make_budget(monkeypatch)

    budget.acquire_lock()
    budget.release_lock()

    assert fake.lock_instance.released is True
    assert budget._lock is None


def test_lock_conflict_raises_session_busy_error(monkeypatch):
    budget, _ = make_budget(monkeypatch, available=False)

    with pytest.raises(SessionBusyError):
        budget.acquire_lock()

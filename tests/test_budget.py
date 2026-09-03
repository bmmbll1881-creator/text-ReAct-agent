import pytest

from budget import BudgetExceeded, LocalBudget, RedisBudget, StepBudget


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.expirations[key] = seconds

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex):
        self.values[key] = value
        self.expirations[key] = ex

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.expirations.pop(key, None)


def test_local_budget_blocks_after_limit():
    budget = LocalBudget(2)

    assert [budget.consume(), budget.consume()] == [1, 2]
    with pytest.raises(BudgetExceeded):
        budget.consume()


def test_redis_budget_sets_ttl_and_shares_session(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("budget.redis.from_url", lambda *args, **kwargs: fake)
    first, second = RedisBudget("shared", 2), RedisBudget("shared", 2)

    assert first.consume() == 1
    assert second.consume() == 2
    assert fake.expirations["agent:budget:shared"] == 3600
    with pytest.raises(BudgetExceeded):
        first.consume()


def test_step_budget_stops_before_second_redis_write(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("budget.redis.from_url", lambda *args, **kwargs: fake)
    budget = StepBudget("local", 1)

    assert budget.consume() == 1
    with pytest.raises(BudgetExceeded):
        budget.consume()
    assert fake.values["agent:budget:local"] == 1


def test_history_serialization_and_clear(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("budget.redis.from_url", lambda *args, **kwargs: fake)
    budget = StepBudget("history", 2)

    budget.save_history([{"role": "user", "content": "hello", "ignored": True}])

    assert budget.load_history() == [{"role": "user", "content": "hello"}]
    assert fake.expirations[budget.history_key] == budget.redis.TTL_SECONDS
    budget.consume()
    budget.clear()
    assert budget.local.current == 0
    assert fake.values == {}

"""本地与 Redis 双层步骤预算。"""

import os

import redis


class BudgetExceeded(RuntimeError):
    """执行步骤超过预算时抛出。"""


class LocalBudget:
    """限制单个进程内的执行步数。"""

    def __init__(self, max_steps: int):
        self.max_steps = max_steps
        self.current = 0

    def consume(self) -> int:
        self.current += 1
        if self.current > self.max_steps:
            raise BudgetExceeded(f"本地步骤已用 {self.current} 次，最大允许 {self.max_steps} 次")
        return self.current


class RedisBudget:
    """限制同一会话在多个进程或实例中的总步数。"""

    def __init__(self, session_id: str, max_steps: int):
        self.redis_client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True
        )
        self.key = f"agent:budget:{session_id}"
        self.max_steps = max_steps

    def consume(self) -> int:
        # Redis 的 INCR 是原子操作，可避免并发时遗漏计数。
        current = int(self.redis_client.incr(self.key))
        if current == 1:
            self.redis_client.expire(self.key, 3600)
        if current > self.max_steps:
            raise BudgetExceeded(f"会话步骤已用 {current} 次，最大允许 {self.max_steps} 次")
        return current


class StepBudget:
    """同时检查本地预算和 Redis 会话预算。"""

    def __init__(self, session_id: str, max_steps: int):
        self.local = LocalBudget(max_steps)
        self.redis = RedisBudget(session_id, max_steps)

    def consume(self) -> int:
        # 先检查本地预算，超过时不再发起 Redis 操作。
        self.local.consume()
        return self.redis.consume()

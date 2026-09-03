"""本地与 Redis 双层步骤预算。"""
import json
import os
import redis
from redis.exceptions import LockNotOwnedError


class BudgetExceeded(RuntimeError):
    """执行步骤超过预算时抛出。"""

class SessionBusyError(RuntimeError):
    """会话正在被其他进程占用时抛出。"""

class LocalBudget:
    """限制单个进程内的执行步数。"""

    def __init__(self, max_steps: int):
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        self.max_steps = max_steps
        self.current = 0

    def consume(self) -> int:
        self.current += 1
        if self.current > self.max_steps:
            raise BudgetExceeded(f"本地步骤已用 {self.current} 次，最大允许 {self.max_steps} 次")
        return self.current


class RedisBudget:
    """限制同一会话在多个进程或实例中的总步数。"""
    # Redis 键的过期时间（秒）
    TTL_SECONDS = 3600

    def __init__(self, session_id: str, max_steps: int):
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        self.redis_client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True
        )
        self.key = f"agent:budget:{session_id}"
        self.max_steps = max_steps

    def consume(self) -> int:
        # Redis 的 INCR 是原子操作，可避免并发时遗漏计数。
        current = int(self.redis_client.incr(self.key))
        self.redis_client.expire(self.key, self.TTL_SECONDS)  # 每次递增后都刷新过期时间
        if current > self.max_steps:
            raise BudgetExceeded(f"会话步骤已用 {current} 次，最大允许 {self.max_steps} 次")
        return current


class StepBudget:
    """同时检查本地预算和 Redis 会话预算。"""

    def __init__(self, session_id: str, max_steps: int):
        self.session_id = session_id
        self._lock = None
        self.local = LocalBudget(max_steps)
        self.redis = RedisBudget(session_id, max_steps)
        self.history_key = f"agent:history:{session_id}"

    def consume(self) -> int:
        """先检查本地预算，超过时不再发起 Redis 操作。"""
        self.local.consume()
        return self.redis.consume()

    def load_history(self) -> list[dict]:
        """从 Redis 读取会话历史，键不存在时返回空列表。"""
        raw = self.redis.redis_client.get(self.history_key)
        if raw is None:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    def save_history(self, messages: list[dict]):
        """保存会话历史，只保留 role 和 content 字段，并设置 TTL。"""
        clean_messages = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
            if isinstance(m, dict) and "role" in m and "content" in m
        ]
        self.redis.redis_client.set(
            self.history_key,
            json.dumps(clean_messages, ensure_ascii=False),
            ex=self.redis.TTL_SECONDS, # 与预算 TTL 一致
        )

    def clear_budget(self):
        """删除 Redis 中的预算键。用于清理预算计数（如中断时）。"""
        self.redis.redis_client.delete(self.redis.key)
        self.local.current = 0

    def clear_history(self):
        """删除 Redis 中的会话历史。用于清理历史记录（如中断时）。"""
        self.redis.redis_client.delete(self.history_key)

    def clear(self) -> None:
        """Reset both persisted session keys and the local counter."""
        self.clear_budget()
        self.clear_history()

    def acquire_lock(self, lock_ttl: int = 30, blocking_timeout: int = 5):
        """
        获取会话锁
        :param lock_ttl: 锁的自动过期时间（秒），防止死锁。
        :param blocking_timeout: 等待获取锁的超时时间（秒）。
        """
        if lock_ttl <= 0 or blocking_timeout <= 0:
            raise ValueError("时间必须大于 0")

        lock = self.redis.redis_client.lock(
            f"react-agent:lock:{self.session_id}",
            timeout=lock_ttl,  # 锁的最长持有时间
        )

        if not lock.acquire(blocking=True, blocking_timeout=blocking_timeout):
            raise SessionBusyError(f"会话 {self.session_id} 正忙，请稍后重试")

        self._lock = lock

    def release_lock(self):
        """释放会话锁"""
        if self._lock is not None:
            try:
                self._lock.release()
            except LockNotOwnedError:
                pass # 锁可能已过期被自动释放，忽略此异常
            finally:
                self._lock = None

    def cleanup_on_interrupt(self):
        """CLI 中断时调用：清除预算并删除历史，释放锁。"""
        self.clear_budget()
        self.clear_history()
        self.release_lock()

    def cleanup_on_cancel(self) -> None:
        """服务端取消时调用：仅释放锁，保留历史和预算。"""
        self.release_lock()

"""In-process session budget/history for offline demos; no Redis locking."""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from budget import BudgetExceeded


class MemoryBudget:
    """In-process step budget + message history for a single demo session.

    Thread-safe. ``acquire_lock`` / ``release_lock`` remain no-ops for API
    compatibility; use them only if the caller needs them, they do not guard
    anything inside this class (internal ``RLock`` already does).
    """

    def __init__(self, session_id: str, max_steps: int):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise TypeError("max_steps must be an int")
        if max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")

        self.session_id = session_id
        self.max_steps = max_steps
        self.current = 0
        self.history: list[dict[str, str]] = []
        self._lock = threading.RLock()

    # --- budget ---------------------------------------------------------------

    def consume(self) -> int:
        """Atomically reserve one step and return the new step count."""
        with self._lock:
            if self.current >= self.max_steps:
                raise BudgetExceeded(f"Session exceeded {self.max_steps} steps")
            self.current += 1
            return self.current

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.max_steps - self.current

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.current >= self.max_steps

    def reset(self, max_steps: int | None = None) -> None:
        """Reset step counter (and optionally the limit). History is preserved."""
        if max_steps is not None:
            if isinstance(max_steps, bool) or not isinstance(max_steps, int):
                raise TypeError("max_steps must be an int")
            if max_steps <= 0:
                raise ValueError("max_steps must be a positive integer")
        with self._lock:
            self.current = 0
            if max_steps is not None:
                self.max_steps = max_steps

    # --- history --------------------------------------------------------------

    def load_history(self) -> list[dict[str, str]]:
        """Return a defensive copy of the stored history."""
        with self._lock:
            return [dict(item) for item in self.history]

    def save_history(self, messages: Iterable[Mapping[str, Any]]) -> None:
        """Replace history with a validated, deep-ish copy of ``messages``."""
        if messages is None:
            raise TypeError("messages must be an iterable of mappings")
        cleaned: list[dict[str, str]] = []
        for index, item in enumerate(messages):
            if not isinstance(item, Mapping):
                raise TypeError(f"messages[{index}] must be a mapping")
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                # 与旧实现一致：跳过不完整项，而不是抛错。
                continue
            cleaned.append({"role": role, "content": content})
        with self._lock:
            self.history = cleaned

    def append_history(self, role: str, content: str) -> None:
        """Append a single validated message."""
        if not isinstance(role, str) or not isinstance(content, str):
            raise TypeError("role and content must be strings")
        with self._lock:
            self.history.append({"role": role, "content": content})

    # --- locks (kept for API compatibility) -----------------------------------

    def acquire_lock(self) -> None:
        return None

    def release_lock(self) -> None:
        return None

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> "MemoryBudget":
        self.acquire_lock()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release_lock()
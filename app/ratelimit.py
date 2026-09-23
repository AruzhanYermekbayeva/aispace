"""Простой in-memory rate limiter (скользящее окно).

Сознательное упрощение: работает в пределах одного процесса. Для одного инстанса
внутреннего сервиса этого достаточно; при горизонтальном масштабировании — Redis.

""" 
from __future__ import annotations

import time
from collections import defaultdict, deque

from app.errors import RateLimited

class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, message: str = "Слишком много запросов, попробуйте позже") -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            retry_after = int(self.window - (now - hits[0])) + 1
            raise RateLimited(message, details={"retry_after_seconds": retry_after})
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()

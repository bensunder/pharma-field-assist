"""Async circuit breaker + bounded retry for downstream integrations.

CLOSED --(N consecutive failures)--> OPEN --(reset timeout)--> HALF_OPEN --(1 success)--> CLOSED
                                                                   \\--(1 failure)--> OPEN

While OPEN, calls fail fast with DependencyUnavailableError so a degraded Graph/Veeva tenant can't
pin request threads or burn the SLA; agents catch that and degrade gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from app.core.errors import DependencyUnavailableError

log = logging.getLogger(__name__)
T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class TransientError(Exception):
    """Retryable downstream failure (timeouts, 429, 5xx)."""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        if self._state == BreakerState.OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            return BreakerState.HALF_OPEN
        return self._state

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state, "consecutive_failures": self._failures}

    def reset(self) -> None:
        self._state, self._failures = BreakerState.CLOSED, 0

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        retries: int = 2,
        backoff: float = 0.05,
    ) -> T:
        if self.state == BreakerState.OPEN:
            raise DependencyUnavailableError(f"{self.name} circuit is open", details={"dependency": self.name, "breaker": "open"})
        attempt = 0
        while True:
            try:
                result = await fn()
            except TransientError as exc:
                attempt += 1
                if attempt <= retries and self.state != BreakerState.HALF_OPEN:
                    await asyncio.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                await self._on_failure()
                raise DependencyUnavailableError(
                    f"{self.name} unavailable: {exc}",
                    details={"dependency": self.name, "breaker": str(self.state)},
                ) from exc
            await self._on_success()
            return result

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state != BreakerState.CLOSED or self._failures:
                log.info("breaker_closed", extra={"dependency": self.name})
            self._state, self._failures = BreakerState.CLOSED, 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self.state == BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
                log.warning("breaker_opened", extra={"dependency": self.name, "failures": self._failures})

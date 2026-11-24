"""Fault injection for incident drills (disabled in prod by settings).

Lets the demo reproduce a Sev-2 ("Outlook scheduling failing for all reps") on demand and show the
breaker, graceful degradation, alerting signal and recovery end to end.
"""

from __future__ import annotations

from enum import StrEnum

from app.resilience.breaker import TransientError


class FaultMode(StrEnum):
    NONE = "none"
    ERROR = "error"  # downstream returns 503
    TIMEOUT = "timeout"  # downstream times out


class FaultRegistry:
    def __init__(self) -> None:
        self._faults: dict[str, FaultMode] = {}

    def set(self, dependency: str, mode: FaultMode) -> None:
        if mode == FaultMode.NONE:
            self._faults.pop(dependency, None)
        else:
            self._faults[dependency] = mode

    def get(self, dependency: str) -> FaultMode:
        return self._faults.get(dependency, FaultMode.NONE)

    def snapshot(self) -> dict[str, str]:
        return {k: str(v) for k, v in self._faults.items()}

    def maybe_raise(self, dependency: str) -> None:
        mode = self.get(dependency)
        if mode == FaultMode.ERROR:
            raise TransientError(f"{dependency} returned 503 (injected)")
        if mode == FaultMode.TIMEOUT:
            raise TransientError(f"{dependency} timed out (injected)")

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class Outcome(StrEnum):
    PASS = "pass"
    REDACTED = "redacted"
    AMENDED = "amended"
    ROUTED = "routed"
    BLOCKED = "blocked"


class Verdict(BaseModel):
    rule_id: str
    stage: str  # "input" | "output" | "authz"
    outcome: Outcome
    detail: str = ""


class InputScreen(BaseModel):
    sanitized_text: str
    verdicts: list[Verdict]
    forced_route: str | None = None  # e.g. "pharmacovigilance" / "medical_information"
    blocked: bool = False
    block_message: str | None = None

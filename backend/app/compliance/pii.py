"""PII/PHI scrubbing applied BEFORE any text reaches an LLM, a log line, or the audit store.

Detection is pattern-based (deterministic, auditable, zero-latency). In production this layer is
the first pass; Azure AI Language PII detection can be chained behind the same interface for
free-text names. Only the *types* found are recorded, never the values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")),
    ("MRN", re.compile(r"\bMRN[:#\s]*\d{5,10}\b", re.IGNORECASE)),
    (
        "DOB",
        re.compile(
            r"\b(?:DOB|date of birth|born(?: on)?)[:\s]*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
            re.IGNORECASE,
        ),
    ),
    # "patient John Smith" / "patient named Mary Jones" -> name after the word patient
    (
        "PATIENT_NAME",
        re.compile(r"(?<=\bpatient )(?:named |called )?[A-Z][a-z]+ [A-Z][a-z]+"),
    ),
]


@dataclass(frozen=True)
class ScrubResult:
    text: str
    found: tuple[str, ...]


def scrub(text: str) -> ScrubResult:
    found: list[str] = []
    out = text
    for label, pattern in _PATTERNS:
        out, n = pattern.subn(f"[{label}]", out)
        if n:
            found.append(label)
    return ScrubResult(text=out, found=tuple(found))

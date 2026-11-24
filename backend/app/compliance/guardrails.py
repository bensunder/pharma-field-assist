"""Compliance guardrails — the policy enforcement points (PEPs) around every agent turn.

Input stage (before routing):
  CMP-001  PII/PHI scrubbing                       -> REDACTED
  CMP-002  Prompt-injection detection              -> BLOCKED
  CMP-003  Adverse-event signal                    -> ROUTED to pharmacovigilance (overrides intent)
  CMP-004  Off-label request                       -> ROUTED to medical information

Output stage (after the agent answers):
  CMP-101  Product claims must be grounded (cited)  -> BLOCKED if uncited
  CMP-102  Prohibited / superlative claims          -> AMENDED (sentence removed)
  CMP-103  Fair balance (ISI) on product content    -> AMENDED (ISI appended)

The policy itself is data (``data/compliance_policy.json``) so Compliance can change wording and
signals through a PR without a code change; the active policy version is stamped on every audit
record.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.compliance.models import InputScreen, Outcome, Verdict
from app.compliance.pii import scrub
from app.config import BACKEND_ROOT

POLICY_PATH = BACKEND_ROOT / "data" / "compliance_policy.json"


class Policy(BaseModel):
    version: str
    approved_products: dict[str, dict]
    off_label_signals: list[str]
    adverse_event_signals: list[str]  # strong: trigger on their own
    adverse_event_weak_signals: list[str]  # trigger only with patient context
    ae_context_signals: list[str]
    prompt_injection_signals: list[str]
    prohibited_claims: list[str]
    ae_reporting_window_hours: int
    medical_information_contact: str


@lru_cache
def load_policy(path: Path = POLICY_PATH) -> Policy:
    return Policy.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _contains_any(text: str, signals: list[str]) -> str | None:
    lowered = text.lower()
    for s in signals:
        if s in lowered:
            return s
    return None


def mentioned_products(text: str, policy: Policy) -> list[str]:
    lowered = text.lower()
    hits = []
    for key, meta in policy.approved_products.items():
        generic = meta["display_name"].split("(")[-1].rstrip(")").strip().lower()
        if key in lowered or generic in lowered:
            hits.append(key)
    return hits


def detect_adverse_event(text: str, policy: Policy) -> str | None:
    if sig := _contains_any(text, policy.adverse_event_signals):
        return sig
    weak = _contains_any(text, policy.adverse_event_weak_signals)
    if weak and (ctx := _contains_any(text, policy.ae_context_signals)):
        return f"{weak} + {ctx.strip()}"
    return None


def screen_input(text: str, policy: Policy | None = None) -> InputScreen:
    policy = policy or load_policy()
    verdicts: list[Verdict] = []

    scrubbed = scrub(text)
    verdicts.append(
        Verdict(
            rule_id="CMP-001",
            stage="input",
            outcome=Outcome.REDACTED if scrubbed.found else Outcome.PASS,
            detail=f"redacted: {', '.join(scrubbed.found)}" if scrubbed.found else "no PII/PHI detected",
        )
    )
    clean = scrubbed.text

    if sig := _contains_any(clean, policy.prompt_injection_signals):
        verdicts.append(Verdict(rule_id="CMP-002", stage="input", outcome=Outcome.BLOCKED, detail=f"signal '{sig}'"))
        return InputScreen(
            sanitized_text=clean,
            verdicts=verdicts,
            blocked=True,
            block_message=(
                "This request was blocked by platform policy (CMP-002). "
                "If you believe this is an error, contact the Field Systems help desk."
            ),
        )
    verdicts.append(Verdict(rule_id="CMP-002", stage="input", outcome=Outcome.PASS))

    # Adverse events take precedence over everything else: a rep who hears about a patient
    # reaction has a regulatory reporting obligation regardless of what they actually asked.
    # Strong signals fire alone; weak ones ("side effect") need patient context so a general
    # label question ("common side effects?") is answered from the ISI instead.
    if sig := detect_adverse_event(clean, policy):
        verdicts.append(
            Verdict(
                rule_id="CMP-003",
                stage="input",
                outcome=Outcome.ROUTED,
                detail=f"adverse-event signal '{sig}' -> pharmacovigilance",
            )
        )
        return InputScreen(sanitized_text=clean, verdicts=verdicts, forced_route="pharmacovigilance")
    verdicts.append(Verdict(rule_id="CMP-003", stage="input", outcome=Outcome.PASS))

    if mentioned_products(clean, policy) and (sig := _contains_any(clean, policy.off_label_signals)):
        verdicts.append(
            Verdict(
                rule_id="CMP-004",
                stage="input",
                outcome=Outcome.ROUTED,
                detail=f"off-label signal '{sig}' -> medical information",
            )
        )
        return InputScreen(sanitized_text=clean, verdicts=verdicts, forced_route="medical_information")
    verdicts.append(Verdict(rule_id="CMP-004", stage="input", outcome=Outcome.PASS))

    return InputScreen(sanitized_text=clean, verdicts=verdicts)


class OutputReview(BaseModel):
    text: str
    verdicts: list[Verdict]
    blocked: bool = False


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def review_output(
    answer: str,
    *,
    is_product_content: bool,
    citations: list[str],
    isi_text: dict[str, str],
    policy: Policy | None = None,
) -> OutputReview:
    """Post-generation review. ``isi_text`` maps product key -> Important Safety Information."""
    policy = policy or load_policy()
    verdicts: list[Verdict] = []

    if is_product_content and not citations:
        verdicts.append(
            Verdict(
                rule_id="CMP-101",
                stage="output",
                outcome=Outcome.BLOCKED,
                detail="product content without approved-source citation",
            )
        )
        return OutputReview(
            text=(
                "I can only share product information that is grounded in approved, MLR-reviewed "
                "content, and I couldn't find an approved source for that question. "
                f"Please submit it to {policy.medical_information_contact}."
            ),
            verdicts=verdicts,
            blocked=True,
        )
    verdicts.append(Verdict(rule_id="CMP-101", stage="output", outcome=Outcome.PASS, detail=f"{len(citations)} citation(s)"))

    kept, removed = [], []
    for sentence in _SENTENCE_SPLIT.split(answer):
        if _contains_any(sentence, policy.prohibited_claims):
            removed.append(sentence)
        else:
            kept.append(sentence)
    text = " ".join(kept).strip()
    verdicts.append(
        Verdict(
            rule_id="CMP-102",
            stage="output",
            outcome=Outcome.AMENDED if removed else Outcome.PASS,
            detail=f"removed {len(removed)} non-compliant sentence(s)" if removed else "",
        )
    )

    if is_product_content:
        products = mentioned_products(answer + " " + " ".join(citations), policy)
        appended = []
        for p in products:
            isi = isi_text.get(p)
            if isi and isi[:60] not in text:
                text += f"\n\n**Important Safety Information — {policy.approved_products[p]['display_name']}:** {isi}"
                appended.append(p)
        verdicts.append(
            Verdict(
                rule_id="CMP-103",
                stage="output",
                outcome=Outcome.AMENDED if appended else Outcome.PASS,
                detail=f"fair balance appended for {', '.join(appended)}" if appended else "",
            )
        )

    return OutputReview(text=text, verdicts=verdicts)

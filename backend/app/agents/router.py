"""Intent router.

Two-tier: an LLM classifier (JSON mode, temperature 0, closed label set) when a model is
configured, with a deterministic weighted-keyword classifier as both the offline mode and the
fallback when the model errors or returns an unknown label. Low-confidence results go to the
``clarify`` path instead of guessing — a mis-routed write (e.g. logging a CRM call) is worse than
a follow-up question.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DependencyUnavailableError
from app.llm.provider import LLMProvider, complete_json

log = logging.getLogger(__name__)

INTENTS = ("product_qa", "scheduling", "analytics", "crm", "help")
CONFIDENCE_FLOOR = 0.35

_RULES: dict[str, list[tuple[str, float]]] = {
    "scheduling": [
        (r"\bschedul", 2.0),
        (r"\bmeeting\b", 1.5),
        (r"\bbook\b", 1.2),
        (r"\bcalendar\b", 1.5),
        (r"\bavailab", 1.0),
        (r"\bfree\b", 1.4),
        (r"\bappointment\b", 1.2),
        (r"\b(?:tomorrow|monday|tuesday|wednesday|thursday|friday)\b", 0.6),
        (r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", 0.6),
        (r"\boutlook\b", 1.0),
    ],
    "analytics": [
        (r"\btrx\b", 2.0),
        (r"\bnrx\b", 2.0),
        (r"\bmarket share\b", 2.0),
        (r"\bprescriptions?\b", 1.4),
        (r"\btrend", 1.0),
        (r"\bperformance\b", 1.0),
        (r"\bterritor", 0.8),
        (r"\bsales\b", 1.0),
        (r"\b(?:last|past) \d+ weeks?\b", 1.0),
        (r"\bhow (?:am i|are we) doing\b", 1.2),
        (r"\brank", 0.8),
    ],
    "crm": [
        # explicit write verbs dominate topical words in the same sentence
        (r"\baccounts?\b", 1.6),
        (r"\blog (?:a )?call\b", 5.0),
        (r"\bcall notes?\b", 2.0),
        (r"\bveeva\b", 1.5),
        (r"\bcrm\b", 1.5),
        (r"\bmy (?:hcps|doctors|targets)\b", 1.5),
        (r"\blast call\b", 1.5),
        (r"\bsegment\b", 1.0),
    ],
    "product_qa": [
        (r"\bdos(?:e|ing|age)\b", 1.6),
        (r"\bindicat", 1.6),
        (r"\bcontraindicat", 2.0),
        (r"\bsafety\b", 1.2),
        (r"\befficacy\b", 1.5),
        (r"\binteraction", 1.5),
        (r"\bcardivex\b", 1.0),
        (r"\bvelostatin\b", 1.0),
        (r"\brespira\b", 1.0),
        (r"\btiolanide\b", 1.0),
        (r"\bformulary\b", 1.4),
        (r"\bcopay\b", 1.4),
        (r"\bsamples?\b", 1.2),
        (r"\bpolicy\b", 1.0),
        (r"\bmeal", 1.0),
        (r"\bspeaker program", 1.4),
        (r"\bcoverage\b", 1.2),
        (r"\bstrengths?\b", 1.0),
        (r"\bprior auth", 1.4),
    ],
    "help": [(r"\bwhat can you do\b", 3.0), (r"^\s*(?:hi|hello|hey|help)\b", 2.0)],
}
_COMPILED = {k: [(re.compile(p, re.IGNORECASE), w) for p, w in v] for k, v in _RULES.items()}


class RouteDecision(BaseModel):
    intent: str
    confidence: float
    method: str  # rules | llm | forced:<rule> | llm_fallback_rules
    rationale: str = ""


def classify_rules(message: str) -> RouteDecision:
    scores = {intent: sum(w for rx, w in rules if rx.search(message)) for intent, rules in _COMPILED.items()}
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    total = sum(scores.values())
    if scores[best] == 0:
        return RouteDecision(intent="clarify", confidence=0.0, method="rules", rationale="no intent signals")
    confidence = round(scores[best] / total, 2) if total else 0.0
    # Absolute floor so a single weak hit doesn't look "confident"
    confidence = round(min(confidence, scores[best] / 2.0), 2)
    intent = best if confidence >= CONFIDENCE_FLOOR else "clarify"
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return RouteDecision(
        intent=intent,
        confidence=confidence,
        method="rules",
        rationale=", ".join(f"{k}={v:.1f}" for k, v in top),
    )


_SYSTEM = (
    "You are the intent router for a pharmaceutical field-sales assistant. Classify the user's "
    f"message into exactly one of: {', '.join(INTENTS)}. "
    "product_qa = approved product/label/access/field-policy questions; scheduling = Outlook "
    "calendar availability or booking; analytics = TRx/NRx/market share/territory performance; "
    "crm = Veeva accounts, HCP lookups, logging calls; help = greetings or capability questions. "
    'Respond with a JSON object only: {"intent": "<label>", "confidence": <0..1>, "rationale": "<max 15 words>"}.'
)

ROUTER_MAX_TOKENS = 80


class RouterOutput(BaseModel):
    """Schema the router's model reply must match; anything else -> rules fallback."""

    model_config = ConfigDict(extra="ignore")
    intent: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=400)


async def route(message: str, llm: LLMProvider) -> RouteDecision:
    rules = classify_rules(message)
    if not llm.enabled:
        return rules
    try:
        out = (await complete_json(llm, _SYSTEM, message, schema=RouterOutput, max_tokens=ROUTER_MAX_TOKENS)).data
        intent, conf = out.intent.strip(), out.confidence
        if intent not in INTENTS:
            raise ValueError(f"unknown intent {intent!r}")
        if conf < CONFIDENCE_FLOOR:
            intent = "clarify"
        return RouteDecision(intent=intent, confidence=round(conf, 2), method="llm", rationale=out.rationale[:200])
    except (DependencyUnavailableError, ValueError, TypeError) as exc:
        log.warning("router_llm_fallback", extra={"reason": str(exc)})
        return rules.model_copy(update={"method": "llm_fallback_rules"})

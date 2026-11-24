import json
from datetime import date, time, timedelta

import pytest

from app.agents.analytics import parse_metric, parse_weeks, resolve_scope
from app.agents.router import classify_rules, route
from app.agents.scheduling import parse_day, parse_duration, parse_time
from app.core.errors import AuthorizationError
from app.security.auth import DEMO_PERSONAS, Principal
from tests.conftest import FakeLLM


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("What is the starting dose of Cardivex?", "product_qa"),
        ("Respira contraindications", "product_qa"),
        ("What's the meal limit for HCPs?", "product_qa"),
        ("When am I free Thursday?", "scheduling"),
        ("Book a meeting with Dr. Patel tomorrow at 2pm", "scheduling"),
        ("Show my Cardivex TRx for the last 6 weeks", "analytics"),
        ("How is my market share trending?", "analytics"),
        ("show my accounts", "crm"),
        ("log a call with Dr. Okafor: discussed Cardivex formulary coverage", "crm"),
        ("hello", "help"),
        ("blah blah", "clarify"),
    ],
)
def test_rules_router(message, intent):
    assert classify_rules(message).intent == intent


async def test_llm_router_used_when_valid():
    llm = FakeLLM(lambda s, u: json.dumps({"intent": "analytics", "confidence": 0.9, "rationale": "rx"}))
    d = await route("how are we doing", llm)
    assert (d.intent, d.method) == ("analytics", "llm")


@pytest.mark.parametrize(
    "llm",
    [
        FakeLLM(down=True),
        FakeLLM(lambda s, u: "not json"),
        FakeLLM(lambda s, u: json.dumps({"intent": "delete_everything", "confidence": 0.99})),
    ],
    ids=["outage", "garbage", "unknown-label"],
)
async def test_llm_router_falls_back_to_rules(llm):
    d = await route("When am I free Thursday?", llm)
    assert d.intent == "scheduling"
    assert d.method == "llm_fallback_rules"


async def test_llm_low_confidence_goes_to_clarify():
    llm = FakeLLM(lambda s, u: '```json\n{"intent": "crm", "confidence": 0.2}\n```')
    assert (await route("hmm", llm)).intent == "clarify"


MONDAY = date(2026, 11, 23)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", MONDAY),
        ("tomorrow at 3", MONDAY + timedelta(days=1)),
        ("thursday", date(2026, 11, 26)),
        ("next monday", date(2026, 11, 30)),  # same weekday -> next week, never today
        ("on 2026-12-02", date(2026, 12, 2)),
        ("sometime", None),
    ],
)
def test_parse_day(text, expected):
    assert parse_day(text, MONDAY) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2pm", time(14, 0)),
        ("at 9:30 am", time(9, 30)),
        ("12pm", time(12, 0)),
        ("12am", time(0, 0)),
        ("14:15", time(14, 15)),
        ("13pm", None),
        ("no time", None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(("text", "minutes"), [("for 45 minutes", 45), ("1 hour", 60), ("quick", 30), ("for 600 min", 240)])
def test_parse_duration(text, minutes):
    assert parse_duration(text) == timedelta(minutes=minutes)


@pytest.mark.parametrize(
    ("text", "metric", "weeks"),
    [("NRx last 4 weeks", "nrx", 4), ("market share this quarter", "market_share", 12), ("trx", "trx", 8), ("past 40 weeks", "trx", 12)],
)
def test_analytics_parsers(text, metric, weeks):
    assert parse_metric(text) == metric
    assert parse_weeks(text) == weeks


def _p(persona: str) -> Principal:
    d = DEMO_PERSONAS[persona]
    return Principal(oid=d["oid"], name=d["name"], email=d["email"], roles=frozenset(d["roles"]), territory=d["territory"])


@pytest.mark.parametrize(
    ("persona", "requested", "expected"),
    [
        ("rep", None, "TX-HOU-03"),
        ("rep", "tx-hou-03", "TX-HOU-03"),
        ("manager", "TX-HOU-01", "TX-HOU-01"),
        ("manager", None, "TX-HOU"),
        ("compliance", "TX-DAL", "TX-DAL"),
    ],
)
def test_scope_allowed(persona, requested, expected):
    assert resolve_scope(requested, _p(persona)) == expected


@pytest.mark.parametrize(("persona", "requested"), [("rep", "TX-HOU-01"), ("rep", "TX-HOU"), ("manager", "TX-DAL-01")])
def test_scope_denied(persona, requested):
    with pytest.raises(AuthorizationError) as exc:
        resolve_scope(requested, _p(persona))
    assert exc.value.details["rule_id"] == "AUTHZ-201"

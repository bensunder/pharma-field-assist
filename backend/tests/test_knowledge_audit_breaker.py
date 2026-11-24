import asyncio
import sqlite3
from datetime import date

import pytest

from app.audit.store import AuditEvent, AuditStore
from app.config import BACKEND_ROOT
from app.core.errors import DependencyUnavailableError
from app.knowledge.index import KnowledgeIndex, stem
from app.resilience.breaker import BreakerState, CircuitBreaker, TransientError

KB = BACKEND_ROOT / "knowledge"


# ------------------------------------------------------------------------------- knowledge
def test_expired_content_never_indexed():
    idx = KnowledgeIndex.from_directory(KB, today=date(2026, 11, 23))
    expired = next(d for d in idx.documents if d.doc_id == "cardivex-launch-messaging-2025")
    assert not expired.indexed and "expired" in expired.exclusion_reason
    assert all(c.doc_id != expired.doc_id for c in idx.chunks)


def test_missing_mlr_id_excluded(tmp_path):
    (tmp_path / "rogue.md").write_text("---\ntitle: Rogue\nproduct: cardivex\n---\n## Claim\nUnreviewed claim text.\n")
    (tmp_path / "ok.md").write_text("---\ntitle: Ok\nmlr_id: MLR-1\n---\n## Dosing\nTake one tablet.\n")
    idx = KnowledgeIndex.from_directory(tmp_path)
    assert [d.doc_id for d in idx.documents if d.indexed] == ["ok"]
    assert idx.search("unreviewed claim") == []


def test_kb_version_changes_with_content(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: A\nmlr_id: M1\n---\n## S\nalpha\n")
    v1 = KnowledgeIndex.from_directory(tmp_path).version
    (tmp_path / "a.md").write_text("---\ntitle: A\nmlr_id: M1\n---\n## S\nbeta\n")
    assert KnowledgeIndex.from_directory(tmp_path).version != v1


@pytest.mark.parametrize(
    ("a", "b"), [("dose", "dosing"), ("dosed", "dosage"), ("indicated", "indication"), ("contraindications", "contraindicated")]
)
def test_stemmer_conflates_label_vocabulary(a, b):
    assert stem(a) == stem(b)


@pytest.mark.parametrize(
    ("query", "product", "expected_section"),
    [
        ("starting dose", "cardivex", "Dosage and Administration"),
        ("contraindications", "cardivex", "Contraindications"),
        ("prior authorization", "cardivex", "Formulary Coverage"),
        ("inhalations per day", "respira", "Dosage and Administration"),
        ("meal limit", None, "Meals and Hospitality"),
    ],
)
def test_retrieval_top_hit(query, product, expected_section):
    idx = KnowledgeIndex.from_directory(KB)
    hits = idx.search(query, product=product, top_k=1)
    assert hits and hits[0].chunk.section == expected_section


def test_product_filter_never_crosses_products():
    idx = KnowledgeIndex.from_directory(KB)
    assert all(h.chunk.product in ("respira", None) for h in idx.search("dose contraindications", product="respira", top_k=10))


def test_product_name_alone_is_not_a_match():
    idx = KnowledgeIndex.from_directory(KB)
    assert idx.search("cardivex zebra", non_topical={"cardivex"}) == []


# ------------------------------------------------------------------------------- audit
def _event(i: int) -> AuditEvent:
    return AuditEvent(correlation_id=f"c{i}", actor_oid="u1", actor_roles=["FieldRep"], event_type="chat.turn", payload={"i": i})


async def test_audit_chain_valid_and_ordered(tmp_path):
    store = AuditStore(tmp_path / "a.db")
    stored = [await store.append(_event(i)) for i in range(5)]
    assert stored[1].prev_hash == stored[0].hash
    v = await store.verify()
    assert v.valid and v.records_checked == 5 and v.head_hash == stored[-1].hash
    assert [e.payload["i"] for e in await store.list(limit=2)] == [4, 3]


async def test_audit_concurrent_appends_keep_chain_intact(tmp_path):
    store = AuditStore(tmp_path / "a.db")
    await asyncio.gather(*(store.append(_event(i)) for i in range(50)))
    assert (await store.verify()).valid


async def test_audit_rejects_update_and_delete(tmp_path):
    store = AuditStore(tmp_path / "a.db")
    await store.append(_event(1))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE audit_events SET payload='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM audit_events")


async def test_audit_detects_out_of_band_tampering(tmp_path):
    path = tmp_path / "a.db"
    store = AuditStore(path)
    for i in range(3):
        await store.append(_event(i))
    raw = sqlite3.connect(path)  # attacker with file access drops the trigger and edits a row
    raw.execute("DROP TRIGGER audit_no_update")
    raw.execute("""UPDATE audit_events SET payload='{"i": 99}' WHERE seq=2""")
    raw.commit()
    raw.close()
    v = await store.verify()
    assert not v.valid and v.first_broken_seq == 2


# ------------------------------------------------------------------------------- breaker
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _failing():
    async def fn():
        raise TransientError("503")

    return fn


async def _ok():
    return "ok"


async def test_breaker_retries_then_opens_then_fails_fast():
    clock = Clock()
    b = CircuitBreaker("graph", failure_threshold=2, reset_timeout=10, clock=clock)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        raise TransientError("503")

    for _ in range(2):
        with pytest.raises(DependencyUnavailableError):
            await b.call(flaky, retries=2, backoff=0)
    assert attempts == 6  # 1 + 2 retries per call
    assert b.state == BreakerState.OPEN
    with pytest.raises(DependencyUnavailableError) as exc:
        await b.call(flaky, backoff=0)
    assert exc.value.details["breaker"] == "open"
    assert attempts == 6  # failed fast, downstream not called


async def test_breaker_half_open_recovers_on_success():
    clock = Clock()
    b = CircuitBreaker("crm", failure_threshold=1, reset_timeout=10, clock=clock)
    with pytest.raises(DependencyUnavailableError):
        await b.call(_failing(), retries=0)
    clock.t = 11
    assert b.state == BreakerState.HALF_OPEN
    assert await b.call(_ok) == "ok"
    assert b.state == BreakerState.CLOSED


async def test_breaker_half_open_reopens_on_failure():
    clock = Clock()
    b = CircuitBreaker("crm", failure_threshold=1, reset_timeout=10, clock=clock)
    with pytest.raises(DependencyUnavailableError):
        await b.call(_failing(), retries=0)
    clock.t = 11
    with pytest.raises(DependencyUnavailableError):
        await b.call(_failing(), retries=3, backoff=0)
    assert b.state == BreakerState.OPEN


async def test_breaker_success_resets_failure_count():
    b = CircuitBreaker("x", failure_threshold=2)
    with pytest.raises(DependencyUnavailableError):
        await b.call(_failing(), retries=0)
    await b.call(_ok)
    with pytest.raises(DependencyUnavailableError):
        await b.call(_failing(), retries=0)
    assert b.state == BreakerState.CLOSED

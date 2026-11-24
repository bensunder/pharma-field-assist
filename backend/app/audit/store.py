"""Tamper-evident audit log.

* Append-only: SQLite triggers reject UPDATE/DELETE at the storage layer.
* Hash-chained: each record stores sha256(prev_hash || canonical_json(record)); ``verify()``
  recomputes the chain so any edit made outside the app (e.g. directly on the file) is detected.
* Only sanitized (PII-scrubbed) text is ever persisted.

Production target is Azure SQL / Postgres with the same schema, plus a nightly export of the chain
head to immutable Blob storage (WORM policy) so the head itself cannot be rewritten. The interface
here is what the rest of the app depends on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    ts              TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    actor_oid       TEXT NOT NULL,
    actor_roles     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_events(actor_oid);
CREATE INDEX IF NOT EXISTS ix_audit_corr ON audit_events(correlation_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    correlation_id: str
    actor_oid: str
    actor_roles: list[str]
    event_type: str  # chat.turn | chat.blocked | ae.flagged | schedule.created | admin.fault ...
    payload: dict[str, Any]


class StoredAuditEvent(AuditEvent):
    seq: int
    prev_hash: str
    hash: str


class ChainVerification(BaseModel):
    valid: bool
    records_checked: int
    first_broken_seq: int | None = None
    head_hash: str


def _canonical(event: AuditEvent) -> str:
    return json.dumps(event.model_dump(), sort_keys=True, separators=(",", ":"), default=str)


def _chain_hash(prev_hash: str, event: AuditEvent) -> str:
    return hashlib.sha256((prev_hash + _canonical(event)).encode()).hexdigest()


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()  # serialises chain-head read + insert
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -------------------------------------------------------------------
    def _append_sync(self, event: AuditEvent) -> StoredAuditEvent:
        with self._lock:
            row = self._conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS_HASH
            digest = _chain_hash(prev, event)
            cur = self._conn.execute(
                "INSERT INTO audit_events (event_id, ts, correlation_id, actor_oid, actor_roles, "
                "event_type, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.ts,
                    event.correlation_id,
                    event.actor_oid,
                    json.dumps(event.actor_roles),
                    event.event_type,
                    json.dumps(event.payload, sort_keys=True, default=str),
                    prev,
                    digest,
                ),
            )
            return StoredAuditEvent(**event.model_dump(), seq=cur.lastrowid, prev_hash=prev, hash=digest)

    async def append(self, event: AuditEvent) -> StoredAuditEvent:
        return await asyncio.to_thread(self._append_sync, event)

    # -- reads --------------------------------------------------------------------
    @staticmethod
    def _row_to_event(row: tuple) -> StoredAuditEvent:
        seq, event_id, ts, corr, oid, roles, etype, payload, prev, digest = row
        return StoredAuditEvent(
            seq=seq,
            event_id=event_id,
            ts=ts,
            correlation_id=corr,
            actor_oid=oid,
            actor_roles=json.loads(roles),
            event_type=etype,
            payload=json.loads(payload),
            prev_hash=prev,
            hash=digest,
        )

    def _list_sync(self, limit: int, event_type: str | None) -> list[StoredAuditEvent]:
        sql = "SELECT seq,event_id,ts,correlation_id,actor_oid,actor_roles,event_type,payload,prev_hash,hash FROM audit_events"
        params: tuple = ()
        if event_type:
            sql += " WHERE event_type = ?"
            params = (event_type,)
        sql += " ORDER BY seq DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_event(r) for r in rows]

    async def list(self, limit: int = 100, event_type: str | None = None) -> list[StoredAuditEvent]:
        return await asyncio.to_thread(self._list_sync, limit, event_type)

    def _verify_sync(self) -> ChainVerification:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,event_id,ts,correlation_id,actor_oid,actor_roles,event_type,payload,prev_hash,hash "
                "FROM audit_events ORDER BY seq ASC"
            ).fetchall()
        prev = GENESIS_HASH
        for row in rows:
            stored = self._row_to_event(row)
            event = AuditEvent(**stored.model_dump(exclude={"seq", "prev_hash", "hash"}))
            if stored.prev_hash != prev or _chain_hash(prev, event) != stored.hash:
                return ChainVerification(valid=False, records_checked=stored.seq, first_broken_seq=stored.seq, head_hash=prev)
            prev = stored.hash
        return ChainVerification(valid=True, records_checked=len(rows), head_hash=prev)

    async def verify(self) -> ChainVerification:
        return await asyncio.to_thread(self._verify_sync)

    def ping(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone() == (1,)

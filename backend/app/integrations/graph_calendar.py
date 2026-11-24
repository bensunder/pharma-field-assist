"""Microsoft Graph / Outlook calendar integration.

``CalendarClient`` holds the scheduling logic (free slots, conflicts) over an abstract ``get_busy``.

* ``GraphCalendarClient`` — LIVE. Microsoft Graph v1.0:
    GET  {base}/calendarView   free/busy for a day (Prefer: outlook.timezone)
    POST {base}/events         create, with ``transactionId`` so a retried POST never double-books
  ``base`` is ``/me`` with a delegated OBO token (auth_mode=entra), or ``/users/{mailbox}`` with an
  app-only token when the demo runs on personas (no user token exists to exchange).
* ``MockCalendarClient`` — in-memory calendars for offline demo and tests.

Every call goes through the "graph" circuit breaker and honours injected faults.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.config import AuthMode, Settings
from app.core.errors import DependencyUnavailableError
from app.integrations import http_errors
from app.resilience.breaker import CircuitBreaker
from app.resilience.faults import FaultRegistry
from app.security.app_tokens import GRAPH_DEFAULT_SCOPE, ClientCredentialsProvider
from app.security.obo import DelegatedToken

log = logging.getLogger(__name__)

CENTRAL = ZoneInfo("America/Chicago")
WORKDAY_START, WORKDAY_END = time(8, 0), time(17, 0)
GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    subject: str
    start: datetime
    end: datetime
    attendees: tuple[str, ...] = ()
    location: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "subject": self.subject,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "attendees": list(self.attendees),
            "location": self.location,
        }


class CalendarClient(Protocol):
    dependency: str
    breaker: CircuitBreaker
    mode: str

    def identity(self, token: DelegatedToken) -> str: ...
    async def get_busy(self, token: DelegatedToken, day: date) -> list[CalendarEvent]: ...
    async def find_free_slots(self, token: DelegatedToken, day: date, duration: timedelta = ..., limit: int = ...) -> list[datetime]: ...
    async def has_conflict(self, token: DelegatedToken, start: datetime, duration: timedelta) -> CalendarEvent | None: ...
    async def create_event(
        self,
        token: DelegatedToken,
        *,
        subject: str,
        start: datetime,
        duration: timedelta,
        attendees: tuple[str, ...],
        location: str = "",
        transaction_id: str | None = None,
    ) -> CalendarEvent: ...


class _SchedulingMixin:
    """Free-slot and conflict logic shared by live and mock clients (pure, over get_busy)."""

    async def get_busy(self, token: DelegatedToken, day: date) -> list[CalendarEvent]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def find_free_slots(
        self, token: DelegatedToken, day: date, duration: timedelta = timedelta(minutes=30), limit: int = 4
    ) -> list[datetime]:
        busy = await self.get_busy(token, day)
        slots: list[datetime] = []
        cursor = datetime.combine(day, WORKDAY_START, CENTRAL)
        end_of_day = datetime.combine(day, WORKDAY_END, CENTRAL)
        now = datetime.now(CENTRAL)
        while cursor + duration <= end_of_day and len(slots) < limit:
            if cursor > now and all(not (cursor < e.end and cursor + duration > e.start) for e in busy):
                slots.append(cursor)
            cursor += timedelta(minutes=30)
        return slots

    async def has_conflict(self, token: DelegatedToken, start: datetime, duration: timedelta) -> CalendarEvent | None:
        for e in await self.get_busy(token, start.date()):
            if start < e.end and start + duration > e.start:
                return e
        return None


# ============================================================================== live
class GraphCalendarClient(_SchedulingMixin):
    dependency = "graph"
    mode = "live"

    def __init__(
        self,
        settings: Settings,
        breaker: CircuitBreaker,
        faults: FaultRegistry,
        http: httpx.AsyncClient,
        app_tokens: ClientCredentialsProvider | None,
    ) -> None:
        self._s = settings
        self.breaker = breaker
        self._faults = faults
        self._http = http
        self._app_tokens = app_tokens
        self._delegated = settings.auth_mode == AuthMode.ENTRA

    def _mailbox(self, token: DelegatedToken) -> str:
        mbx = self._s.graph_mailbox_map.get(token.subject_oid) or self._s.graph_demo_mailbox
        if not mbx:
            raise DependencyUnavailableError("No Graph mailbox mapped for this user", details={"dependency": "graph"})
        return mbx

    def identity(self, token: DelegatedToken) -> str:
        if self._delegated:
            return f"user:{token.subject_oid} (OBO → Graph /me)"
        return f"app:client-credentials → mailbox {self._mailbox(token)}"

    async def _auth(self, token: DelegatedToken) -> tuple[str, dict[str, str]]:
        if self._delegated:
            return f"{GRAPH}/me", {"Authorization": f"Bearer {token.access_token}"}
        assert self._app_tokens is not None  # guaranteed by Settings.missing_live_config
        app = await self._app_tokens.get(GRAPH_DEFAULT_SCOPE)
        return f"{GRAPH}/users/{self._mailbox(token)}", {"Authorization": f"Bearer {app.access_token}"}

    def _parse_dt(self, value: dict) -> datetime:
        # Graph returns 7 fractional digits ("2026-11-24T08:00:00.0000000") in the Prefer'd timezone
        raw = value["dateTime"].split(".")[0]
        return datetime.fromisoformat(raw).replace(tzinfo=CENTRAL)

    async def get_busy(self, token: DelegatedToken, day: date) -> list[CalendarEvent]:
        start = datetime.combine(day, time(0, 0), CENTRAL).astimezone(UTC)
        end = start + timedelta(days=1)

        async def _op() -> list[CalendarEvent]:
            self._faults.maybe_raise(self.dependency)
            base, headers = await self._auth(token)
            headers = {**headers, "Prefer": f'outlook.timezone="{self._s.graph_timezone}"'}
            url: str | None = f"{base}/calendarView"
            params: dict | None = {
                "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$select": "id,subject,start,end,showAs,isCancelled",
                "$orderby": "start/dateTime",
                "$top": "50",
            }
            events: list[CalendarEvent] = []
            for _ in range(5):  # bounded pagination
                resp = await http_errors.send("graph", lambda u=url, p=params: self._http.get(u, headers=headers, params=p))
                body = resp.json()
                for e in body.get("value", []):
                    if e.get("isCancelled") or e.get("showAs") == "free":
                        continue
                    events.append(
                        CalendarEvent(
                            event_id=e["id"],
                            subject=e.get("subject") or "(busy)",
                            start=self._parse_dt(e["start"]),
                            end=self._parse_dt(e["end"]),
                        )
                    )
                url, params = body.get("@odata.nextLink"), None
                if not url:
                    break
            return sorted(events, key=lambda ev: ev.start)

        return await self.breaker.call(_op)

    async def create_event(
        self,
        token: DelegatedToken,
        *,
        subject: str,
        start: datetime,
        duration: timedelta,
        attendees: tuple[str, ...],
        location: str = "",
        transaction_id: str | None = None,
    ) -> CalendarEvent:
        local_start = start.astimezone(CENTRAL).replace(tzinfo=None)
        local_end = local_start + duration
        body = {
            "subject": subject,
            "body": {
                "contentType": "text",
                "content": f"HCP: {', '.join(attendees) or 'n/a'}\nBooked by PharmaFieldAssist (human-confirmed).",
            },
            "start": {"dateTime": local_start.isoformat(), "timeZone": self._s.graph_timezone},
            "end": {"dateTime": local_end.isoformat(), "timeZone": self._s.graph_timezone},
            "location": {"displayName": location},
            "showAs": "busy",
            "categories": ["PharmaFieldAssist"],
            # Idempotency: Graph rejects a second create with the same transactionId, so a retry
            # after a timeout can never produce a duplicate meeting.
            "transactionId": transaction_id or str(uuid.uuid4()),
        }

        async def _op() -> CalendarEvent:
            self._faults.maybe_raise(self.dependency)
            base, headers = await self._auth(token)
            resp = await http_errors.send("graph", lambda: self._http.post(f"{base}/events", headers=headers, json=body))
            created = resp.json()
            return CalendarEvent(
                event_id=created.get("id", ""),
                subject=created.get("subject", subject),
                start=start,
                end=start + duration,
                attendees=attendees,
                location=location,
            )

        return await self.breaker.call(_op, retries=1)  # safe to retry once thanks to transactionId


# ============================================================================== mock
class MockCalendarClient(_SchedulingMixin):
    dependency = "graph"
    mode = "mock"

    def __init__(self, breaker: CircuitBreaker, faults: FaultRegistry) -> None:
        self.breaker = breaker
        self._faults = faults
        self._calendars: dict[str, list[CalendarEvent]] = {}

    def identity(self, token: DelegatedToken) -> str:
        return f"user:{token.subject_oid} (OBO, mock Graph)"

    def _calendar(self, oid: str) -> list[CalendarEvent]:
        if oid not in self._calendars:
            self._calendars[oid] = _seed_busy_blocks()
        return self._calendars[oid]

    async def get_busy(self, token: DelegatedToken, day: date) -> list[CalendarEvent]:
        async def _op() -> list[CalendarEvent]:
            self._faults.maybe_raise(self.dependency)
            return sorted((e for e in self._calendar(token.subject_oid) if e.start.date() == day), key=lambda e: e.start)

        return await self.breaker.call(_op)

    async def create_event(
        self,
        token: DelegatedToken,
        *,
        subject: str,
        start: datetime,
        duration: timedelta,
        attendees: tuple[str, ...],
        location: str = "",
        transaction_id: str | None = None,
    ) -> CalendarEvent:
        async def _op() -> CalendarEvent:
            self._faults.maybe_raise(self.dependency)
            event = CalendarEvent(
                event_id=f"AAMk-{uuid.uuid4().hex[:16]}",
                subject=subject,
                start=start,
                end=start + duration,
                attendees=attendees,
                location=location,
            )
            self._calendar(token.subject_oid).append(event)
            return event

        return await self.breaker.call(_op, retries=0)


def _seed_busy_blocks() -> list[CalendarEvent]:
    """Two weeks of realistic rep calendar noise starting today (Central time)."""
    today = datetime.now(CENTRAL).date()
    events: list[CalendarEvent] = []
    for offset in range(14):
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        blocks = [(time(8, 0), 30, "District huddle (Teams)")]
        if d.weekday() in (0, 2):
            blocks.append((time(10, 0), 90, "Office calls — Medical Center"))
        if d.weekday() in (1, 3):
            blocks.append((time(12, 0), 60, "Lunch & learn — Heights Family Clinic"))
        blocks.append((time(15, 0), 60, "CRM call notes / admin"))
        for start_t, minutes, subject in blocks:
            start = datetime.combine(d, start_t, CENTRAL)
            events.append(
                CalendarEvent(
                    event_id=f"seed-{d.isoformat()}-{start_t.hour}",
                    subject=subject,
                    start=start,
                    end=start + timedelta(minutes=minutes),
                )
            )
    return events

"""Field scheduling agent (Microsoft Graph / Outlook via OBO).

* "When am I free Thursday?"                     -> read-only free/busy, returns open slots
* "Schedule a meeting with Dr. Patel tomorrow at 2pm" -> conflict check, then a PendingAction that
  the rep must confirm before anything is written to Outlook (human-in-the-loop).

If Graph is degraded (breaker open), the agent says so plainly and tells the rep what still works,
instead of failing the whole turn.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from app.agents.base import AgentContext, AgentResult, PendingAction
from app.core.errors import DependencyUnavailableError
from app.integrations.graph_calendar import CENTRAL
from app.security.obo import CRM_SCOPE, GRAPH_SCOPE

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.IGNORECASE)
_DURATION = re.compile(r"\b(\d{2,3})\s?(?:min|minutes)\b|\b(\d)\s?(?:hour|hr)s?\b", re.IGNORECASE)
_WITH = re.compile(r"\bwith\s+((?:[Dd]r\.?\s+)?[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?)")


def parse_day(text: str, today: date) -> date | None:
    t = text.lower()
    if "today" in t:
        return today
    if "tomorrow" in t:
        return today + timedelta(days=1)
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return date.fromisoformat(m.group(1))
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            # "Thursday" / "next Thursday" both mean the next upcoming Thursday (never today)
            delta = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return None


def parse_time(text: str) -> time | None:
    m = _TIME.search(text)
    if not m:
        return None
    if m.group(3):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return None
        if m.group(3).lower() == "pm" and hour != 12:
            hour += 12
        if m.group(3).lower() == "am" and hour == 12:
            hour = 0
        return time(hour, minute)
    return time(int(m.group(4)), int(m.group(5)))


def parse_duration(text: str) -> timedelta:
    m = _DURATION.search(text)
    if not m:
        return timedelta(minutes=30)
    minutes = int(m.group(1)) if m.group(1) else int(m.group(2)) * 60
    return timedelta(minutes=max(15, min(minutes, 240)))


class SchedulingAgent:
    name = "scheduling"
    description = "Checks Outlook availability and books HCP meetings (confirm before write)"

    async def run(self, ctx: AgentContext) -> AgentResult:
        graph = ctx.services.graph
        today = datetime.now(CENTRAL).date()
        day = parse_day(ctx.message, today)
        at = parse_time(ctx.message)
        duration = parse_duration(ctx.message)

        if day is None and at is not None:
            day = today if datetime.now(CENTRAL).time() < at else today + timedelta(days=1)
        if day is None:
            return AgentResult(
                answer='Which day should I check? For example: *"When am I free Thursday?"* or '
                '*"Book 30 minutes with Dr. Patel tomorrow at 2pm."*'
            )
        if day.weekday() >= 5:
            return AgentResult(answer=f"{day:%A, %b %d} is a weekend. HCP meetings are scheduled Monday to Friday.")

        try:
            token = await ctx.delegated(GRAPH_SCOPE)
            if at is None:
                slots = await ctx.call_tool(
                    "graph.calendarView",
                    "graph",
                    lambda: graph.find_free_slots(token, day, duration),
                    token=token,
                    identity=graph.identity(token),
                )
                if not slots:
                    return AgentResult(answer=f"You have no open {int(duration.total_seconds() // 60)}-minute slots on {day:%A, %b %d}.")
                listing = ", ".join(s.strftime("%-I:%M %p") for s in slots)
                return AgentResult(
                    answer=f"Open {int(duration.total_seconds() // 60)}-minute slots on {day:%A, %b %d} (Central): {listing}.",
                    data={"slots": [s.isoformat() for s in slots]},
                )

            start = datetime.combine(day, at, CENTRAL)
            conflict = await ctx.call_tool(
                "graph.calendarView",
                "graph",
                lambda: graph.has_conflict(token, start, duration),
                token=token,
                identity=graph.identity(token),
            )
        except DependencyUnavailableError:
            return AgentResult(
                answer=(
                    "Outlook scheduling is temporarily unavailable. The platform team has been alerted "
                    "and your request was **not** booked. Product Q&A, CRM and analytics are "
                    "still working. Please try scheduling again in a few minutes."
                ),
                degraded=True,
            )

        if conflict:
            alt = await self._alternatives(ctx, token, day, duration)
            return AgentResult(
                answer=(
                    f"{start:%-I:%M %p} on {day:%A} conflicts with **{conflict.subject}** "
                    f"({conflict.start:%-I:%M}–{conflict.end:%-I:%M %p}). "
                    + (f"Open alternatives: {', '.join(s.strftime('%-I:%M %p') for s in alt)}." if alt else "")
                ),
                data={"slots": [s.isoformat() for s in alt]},
            )

        attendee = await self._resolve_attendee(ctx)
        subject = f"HCP meeting — {attendee['name']}" if attendee else "Field meeting"
        action = PendingAction(
            kind="calendar.create_event",
            summary=f"Create Outlook event '{subject}' on {start:%a %b %d, %-I:%M %p} CT for {int(duration.total_seconds() // 60)} min",
            params={
                "subject": subject,
                "start": start.isoformat(),
                "duration_minutes": int(duration.total_seconds() // 60),
                "attendees": [attendee["name"]] if attendee else [],
                "location": attendee["organization"] if attendee else "",
                "account_id": attendee["account_id"] if attendee else None,
            },
            owner_oid=ctx.principal.oid,
            correlation_id=ctx.correlation_id,
        )
        return AgentResult(
            answer=f"You're free then. Ready to book: **{action.summary}**. Confirm to create it in Outlook.",
            pending_action=action,
        )

    async def _alternatives(self, ctx: AgentContext, token, day: date, duration: timedelta) -> list[datetime]:
        try:
            graph = ctx.services.graph
            return await ctx.call_tool(
                "graph.calendarView",
                "graph",
                lambda: graph.find_free_slots(token, day, duration, limit=3),
                token=token,
                identity=graph.identity(token),
            )
        except DependencyUnavailableError:
            return []

    async def _resolve_attendee(self, ctx: AgentContext) -> dict | None:
        m = _WITH.search(ctx.message)
        if not m:
            return None
        try:
            crm_token = await ctx.delegated(CRM_SCOPE)
            crm = ctx.services.crm
            return await ctx.call_tool(
                "crm.findAccount",
                "crm",
                lambda: crm.find_account(crm_token, ctx.principal.territory, m.group(1)),
                token=crm_token,
                identity=crm.identity(crm_token),
            )
        except DependencyUnavailableError:
            return None  # booking still works without CRM enrichment

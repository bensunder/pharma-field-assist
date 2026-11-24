"""Turn orchestrator — the only place that composes guardrails, routing, agents and audit.

  user msg
    -> [input guardrails]  PII scrub, injection block, AE / off-label forced routes
    -> [router]            LLM or rules -> intent (+ confidence floor -> clarify)
    -> [authz]             role x agent matrix (AUTHZ-1xx), data scope inside agents (AUTHZ-2xx)
    -> [agent]             tool calls via OBO / managed identity, breaker-protected
    -> [output guardrails] grounding, prohibited claims, fair balance
    -> [audit]             hash-chained record of the whole turn
    -> TurnResult (answer + full trace for the UI)

Kept framework-agnostic (plain async) so it can be wrapped by a LangGraph StateGraph node-per-step
later without changing agent code; each step here maps 1:1 to a graph node.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel

from app.agents.analytics import AnalyticsAgent
from app.agents.base import Agent, AgentContext, AgentResult, PendingAction, ToolCall
from app.agents.crm_agent import CrmAgent
from app.agents.product_qa import ProductQAAgent
from app.agents.router import RouteDecision, route
from app.agents.safety import MedicalInformationAgent, PharmacovigilanceAgent
from app.agents.scheduling import SchedulingAgent
from app.audit.store import AuditEvent
from app.compliance.guardrails import load_policy, review_output, screen_input
from app.compliance.models import Outcome, Verdict
from app.core.errors import AuthorizationError, DependencyUnavailableError, ValidationError
from app.security.auth import Principal, Role
from app.security.obo import CRM_SCOPE, GRAPH_SCOPE
from app.services import Services

log = logging.getLogger(__name__)

ALL = frozenset(Role)
FIELD = frozenset({Role.FIELD_REP, Role.DISTRICT_MANAGER})
AGENT_ROLES: dict[str, frozenset[Role]] = {
    "product_qa": ALL,
    "scheduling": FIELD,
    "analytics": FIELD,
    "crm": FIELD,
    "pharmacovigilance": ALL,
    "medical_information": ALL,
    "help": ALL,
    "clarify": ALL,
}

MAX_MESSAGE_CHARS = 2000


class TraceStep(BaseModel):
    step: str
    status: str
    detail: str = ""
    ms: float = 0.0


class PendingActionView(BaseModel):
    action_id: str
    kind: str
    summary: str
    expires_at: datetime


class TurnResult(BaseModel):
    turn_id: str
    correlation_id: str
    conversation_id: str
    answer: str
    intent: str
    agent: str
    route: RouteDecision
    verdicts: list[Verdict]
    tool_calls: list[ToolCall]
    citations: list[str]
    pending_action: PendingActionView | None = None
    data: dict | None = None
    degraded: bool = False
    blocked: bool = False
    trace: list[TraceStep]
    latency_ms: float
    policy_version: str
    kb_version: str
    audit_seq: int
    audit_hash: str


class ActionResult(BaseModel):
    action_id: str
    kind: str
    status: str
    message: str
    tool_calls: list[ToolCall]
    record: dict | None = None
    audit_seq: int


_HELP = (
    "I'm your field assistant. I can:\n"
    "- **Answer product questions** from MLR-approved content (dosing, indications, safety, coverage, field policy)\n"
    "- **Check your Outlook calendar** and book HCP meetings (you confirm before anything is created)\n"
    "- **Show TRx / NRx / market-share trends** for your territory from Fabric\n"
    "- **Look up Veeva accounts** and log calls (you confirm before submission)\n\n"
    "Adverse events and off-label questions are routed automatically to Pharmacovigilance and Medical Information."
)


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.agents: dict[str, Agent] = {
            a.name: a
            for a in (
                ProductQAAgent(),
                SchedulingAgent(),
                AnalyticsAgent(),
                CrmAgent(),
                PharmacovigilanceAgent(),
                MedicalInformationAgent(),
            )
        }

    async def handle_turn(
        self, principal: Principal, message: str, *, correlation_id: str, conversation_id: str | None = None
    ) -> TurnResult:
        started = time.perf_counter()
        message = (message or "").strip()
        if not message:
            raise ValidationError("Message is empty")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValidationError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        policy = load_policy()
        trace: list[TraceStep] = []

        def step(name: str, t0: float, status: str, detail: str = "") -> None:
            trace.append(TraceStep(step=name, status=status, detail=detail, ms=round((time.perf_counter() - t0) * 1000, 2)))

        # 1. input guardrails ---------------------------------------------------------
        t0 = time.perf_counter()
        screen = screen_input(message, policy)
        verdicts = list(screen.verdicts)
        flagged = [v for v in verdicts if v.outcome != Outcome.PASS]
        step("input_guardrails", t0, "flagged" if flagged else "pass", "; ".join(f"{v.rule_id}:{v.outcome}" for v in flagged))

        if screen.blocked:
            decision = RouteDecision(intent="blocked", confidence=1.0, method="forced:CMP-002")
            result = AgentResult(answer=screen.block_message or "Blocked by policy.")
            return await self._finish(
                principal,
                screen.sanitized_text,
                decision,
                "none",
                result,
                verdicts,
                trace,
                started,
                correlation_id,
                conversation_id,
                blocked=True,
            )

        # 2. routing ------------------------------------------------------------------
        t0 = time.perf_counter()
        if screen.forced_route:
            rule = next(v.rule_id for v in verdicts if v.outcome == Outcome.ROUTED)
            decision = RouteDecision(intent=screen.forced_route, confidence=1.0, method=f"forced:{rule}")
        else:
            decision = await route(screen.sanitized_text, self.s.llm)
        step("route", t0, "ok", f"{decision.intent} ({decision.method}, conf {decision.confidence})")

        # 3. authz (role x agent) -------------------------------------------------------
        t0 = time.perf_counter()
        agent_name = decision.intent
        allowed = AGENT_ROLES.get(agent_name, frozenset())
        if not principal.roles & allowed:
            verdicts.append(
                Verdict(rule_id="AUTHZ-101", stage="authz", outcome=Outcome.BLOCKED, detail=f"role not permitted for {agent_name}")
            )
            step("authz", t0, "denied", "AUTHZ-101")
            result = AgentResult(answer=f"Your role doesn't have access to the {agent_name.replace('_', ' ')} capability.")
            return await self._finish(
                principal,
                screen.sanitized_text,
                decision,
                agent_name,
                result,
                verdicts,
                trace,
                started,
                correlation_id,
                conversation_id,
                blocked=True,
            )
        verdicts.append(Verdict(rule_id="AUTHZ-101", stage="authz", outcome=Outcome.PASS))
        step("authz", t0, "pass")

        # 4. agent ----------------------------------------------------------------------
        t0 = time.perf_counter()
        ctx = AgentContext(principal=principal, message=screen.sanitized_text, correlation_id=correlation_id, services=self.s)
        blocked = False
        if agent_name == "help":
            result = AgentResult(answer=_HELP)
        elif agent_name == "clarify":
            result = AgentResult(
                answer="I want to make sure I route this correctly. Is this about a **product question**, "
                "**scheduling**, **territory analytics**, or a **CRM account / call**?"
            )
        else:
            try:
                result = await self.agents[agent_name].run(ctx)
            except AuthorizationError as exc:
                rule_id = exc.details.get("rule_id", "AUTHZ-200")
                verdicts.append(Verdict(rule_id=rule_id, stage="authz", outcome=Outcome.BLOCKED, detail=exc.message))
                result = AgentResult(answer=f"{exc.message}. Data access follows your Entra ID territory assignment.")
                blocked = True
            except DependencyUnavailableError as exc:
                result = AgentResult(answer=f"A downstream service is unavailable ({exc.message}). Please retry shortly.", degraded=True)
            except Exception:
                log.exception("agent_failed", extra={"agent": agent_name})
                result = AgentResult(
                    answer=f"Something went wrong handling that request. Reference: `{correlation_id}`. "
                    "The support team can trace it with this ID.",
                    degraded=True,
                )
        result.tool_calls = ctx.tool_calls + result.tool_calls
        step(
            f"agent:{agent_name}",
            t0,
            "degraded" if result.degraded else ("denied" if blocked else "ok"),
            f"{len(result.tool_calls)} tool call(s)",
        )

        # 5. output guardrails ------------------------------------------------------------
        if not blocked and agent_name == "product_qa":
            t0 = time.perf_counter()
            isi = {p: txt for p in policy.approved_products if (txt := self.s.knowledge.isi_for(p))}
            review = review_output(
                result.answer,
                is_product_content=result.is_product_content,
                citations=result.citations,
                isi_text=isi,
                policy=policy,
            )
            verdicts.extend(review.verdicts)
            result.answer = review.text
            if review.blocked:
                result.citations = []
            out_flags = [v for v in review.verdicts if v.outcome != Outcome.PASS]
            step("output_guardrails", t0, "flagged" if out_flags else "pass", "; ".join(f"{v.rule_id}:{v.outcome}" for v in out_flags))

        if result.pending_action:
            self.s.actions.put(result.pending_action)

        return await self._finish(
            principal,
            screen.sanitized_text,
            decision,
            agent_name,
            result,
            verdicts,
            trace,
            started,
            correlation_id,
            conversation_id,
            blocked=blocked,
        )

    async def _finish(
        self,
        principal: Principal,
        sanitized: str,
        decision: RouteDecision,
        agent_name: str,
        result: AgentResult,
        verdicts: list[Verdict],
        trace: list[TraceStep],
        started: float,
        correlation_id: str,
        conversation_id: str,
        *,
        blocked: bool,
    ) -> TurnResult:
        policy = load_policy()
        latency = round((time.perf_counter() - started) * 1000, 2)
        t0 = time.perf_counter()
        roles = sorted(str(r) for r in principal.roles)
        for event_type, payload in result.audit_events:
            await self.s.audit.append(
                AuditEvent(
                    correlation_id=correlation_id, actor_oid=principal.oid, actor_roles=roles, event_type=event_type, payload=payload
                )
            )
        stored = await self.s.audit.append(
            AuditEvent(
                correlation_id=correlation_id,
                actor_oid=principal.oid,
                actor_roles=roles,
                event_type="chat.blocked" if blocked else "chat.turn",
                payload={
                    "conversation_id": conversation_id,
                    "territory": principal.territory,
                    "input_sanitized": sanitized,
                    "intent": decision.intent,
                    "route_method": decision.method,
                    "agent": agent_name,
                    "verdicts": [v.model_dump() for v in verdicts],
                    "tool_calls": [t.model_dump() for t in result.tool_calls],
                    "citations": result.citations,
                    "answer": result.answer,
                    "pending_action": result.pending_action.kind if result.pending_action else None,
                    "degraded": result.degraded,
                    "policy_version": policy.version,
                    "kb_version": self.s.knowledge.version,
                    "latency_ms": latency,
                },
            )
        )
        trace.append(
            TraceStep(
                step="audit", status="ok", detail=f"seq {stored.seq} · {stored.hash[:10]}…", ms=round((time.perf_counter() - t0) * 1000, 2)
            )
        )
        log.info(
            "chat_turn",
            extra={"intent": decision.intent, "agent": agent_name, "latency_ms": latency, "degraded": result.degraded, "blocked": blocked},
        )
        pa = result.pending_action
        return TurnResult(
            turn_id=stored.event_id,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            answer=result.answer,
            intent=decision.intent,
            agent=agent_name,
            route=decision,
            verdicts=verdicts,
            tool_calls=result.tool_calls,
            citations=result.citations,
            pending_action=PendingActionView(action_id=pa.action_id, kind=pa.kind, summary=pa.summary, expires_at=pa.expires_at)
            if pa
            else None,
            data=result.data,
            degraded=result.degraded,
            blocked=blocked,
            trace=trace,
            latency_ms=latency,
            policy_version=policy.version,
            kb_version=self.s.knowledge.version,
            audit_seq=stored.seq,
            audit_hash=stored.hash,
        )

    # ------------------------------------------------------------------ HITL actions
    async def confirm_action(self, principal: Principal, action_id: str, *, correlation_id: str) -> ActionResult:
        action = self.s.actions.take(action_id, principal.oid)
        ctx = AgentContext(principal=principal, message="", correlation_id=correlation_id, services=self.s)
        record: dict | None
        try:
            record, message = await self._execute(ctx, action)
            status = "completed"
        except DependencyUnavailableError as exc:
            # put it back so the rep can retry once the dependency recovers
            self.s.actions.put(action)
            record, message, status = None, f"Not completed: {exc.message}. You can retry the confirmation.", "failed"
        stored = await self.s.audit.append(
            AuditEvent(
                correlation_id=correlation_id,
                actor_oid=principal.oid,
                actor_roles=sorted(str(r) for r in principal.roles),
                event_type=f"action.{status}",
                payload={
                    "action_id": action.action_id,
                    "kind": action.kind,
                    "origin_correlation_id": action.correlation_id,
                    "params": action.params,
                    "record": record,
                    "tool_calls": [t.model_dump() for t in ctx.tool_calls],
                },
            )
        )
        return ActionResult(
            action_id=action.action_id,
            kind=action.kind,
            status=status,
            message=message,
            tool_calls=ctx.tool_calls,
            record=record,
            audit_seq=stored.seq,
        )

    async def cancel_action(self, principal: Principal, action_id: str, *, correlation_id: str) -> ActionResult:
        action = self.s.actions.take(action_id, principal.oid)
        stored = await self.s.audit.append(
            AuditEvent(
                correlation_id=correlation_id,
                actor_oid=principal.oid,
                actor_roles=sorted(str(r) for r in principal.roles),
                event_type="action.cancelled",
                payload={"action_id": action.action_id, "kind": action.kind},
            )
        )
        return ActionResult(
            action_id=action.action_id,
            kind=action.kind,
            status="cancelled",
            message="Cancelled. Nothing was written.",
            tool_calls=[],
            audit_seq=stored.seq,
        )

    async def _execute(self, ctx: AgentContext, action: PendingAction) -> tuple[dict, str]:
        p = action.params
        if action.kind == "calendar.create_event":
            token = await ctx.delegated(GRAPH_SCOPE)
            event = await ctx.call_tool(
                "graph.createEvent",
                "graph",
                lambda: self.s.graph.create_event(
                    token,
                    subject=p["subject"],
                    start=datetime.fromisoformat(p["start"]),
                    duration=timedelta(minutes=p["duration_minutes"]),
                    attendees=tuple(p["attendees"]),
                    location=p["location"],
                    transaction_id=action.action_id,  # Graph idempotency key == HITL action id
                ),
                token=token,
                identity=self.s.graph.identity(token),
            )
            return event.to_dict(), f"Booked in Outlook: {event.subject}, {event.start:%a %b %d %-I:%M %p} CT."
        if action.kind == "crm.log_call":
            token = await ctx.delegated(CRM_SCOPE)
            rec = await ctx.call_tool(
                "crm.createCall",
                "crm",
                lambda: self.s.crm.log_call(token, ctx.principal.territory, p["account_id"], p["notes"], p["products"]),
                token=token,
                identity=self.s.crm.identity(token),
            )
            return rec, f"Call submitted to Veeva ({rec['call_id']})."
        if action.kind == "medinfo.submit_mir":
            mir = f"MIR-{uuid.uuid4().hex[:8].upper()}"
            return {
                "mir_id": mir,
                "products": p["products"],
            }, f"Medical Information Request {mir} submitted. Medical Affairs will respond to the HCP directly."
        raise ValidationError(f"Unknown action kind {action.kind}")

"""Shared agent contracts.

Agents are pure domain logic: they receive an ``AgentContext`` (principal, sanitized message, tool
handles) and return an ``AgentResult``. They never touch HTTP, the audit store or compliance —
those are orchestration concerns, which keeps every agent unit-testable in isolation.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel, Field

from app.core.errors import AuthorizationError, NotFoundError
from app.security.auth import Principal
from app.security.obo import DelegatedToken

if TYPE_CHECKING:
    from app.services import Services

T = TypeVar("T")


class ToolCall(BaseModel):
    tool: str
    dependency: str
    identity: str  # "user:<oid> (OBO)" or "app:managed-identity"
    scope: str | None = None
    token_fingerprint: str | None = None
    duration_ms: float
    status: str  # ok | error | circuit_open
    detail: str = ""


class PendingAction(BaseModel):
    action_id: str = Field(default_factory=lambda: f"act_{uuid.uuid4().hex[:12]}")
    kind: str  # calendar.create_event | crm.log_call | medinfo.submit_mir
    summary: str
    params: dict[str, Any]
    owner_oid: str
    correlation_id: str
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15))


class AgentResult(BaseModel):
    answer: str
    citations: list[str] = []
    tool_calls: list[ToolCall] = []
    is_product_content: bool = False
    pending_action: PendingAction | None = None
    data: dict[str, Any] | None = None  # structured payload for UI (table/chart/slots)
    degraded: bool = False
    audit_events: list[tuple[str, dict]] = []  # extra domain events for the audit log


@dataclass
class AgentContext:
    principal: Principal
    message: str
    correlation_id: str
    services: Services
    tool_calls: list[ToolCall] = field(default_factory=list)

    async def delegated(self, scope: str) -> DelegatedToken:
        return await self.services.obo.acquire(self.principal, scope)

    async def call_tool(
        self,
        tool: str,
        dependency: str,
        fn: Callable[[], Awaitable[T]],
        *,
        token: DelegatedToken | None = None,
        identity: str | None = None,
        describe: Callable[[T], str] | None = None,
    ) -> T:
        """Execute a tool call and record it in the turn trace, success or failure.

        ``identity`` is supplied by the integration client so the trace shows exactly which
        principal reached the downstream system (OBO user, app-only, JWT-bearer user...).
        """
        from app.core.errors import DependencyUnavailableError  # local import avoids cycle

        identity = identity or (f"user:{self.principal.oid} (OBO)" if token else "app:managed-identity")
        started = time.perf_counter()
        status, detail = "ok", ""
        try:
            result = await fn()
            if describe is not None:
                detail = describe(result)
            return result
        except DependencyUnavailableError as exc:
            status = "circuit_open" if exc.details.get("breaker") == "open" else "error"
            detail = exc.message
            raise
        except Exception as exc:
            status, detail = "error", type(exc).__name__
            raise
        finally:
            self.tool_calls.append(
                ToolCall(
                    tool=tool,
                    dependency=dependency,
                    identity=identity,
                    # app-only calls don't use the user's delegated token; don't attribute its scope to them
                    scope=token.scope if token and not identity.startswith("app:") else None,
                    token_fingerprint=token.fingerprint if token and not identity.startswith("app:") else None,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    status=status,
                    detail=detail,
                )
            )


class Agent(Protocol):
    name: str
    description: str

    async def run(self, ctx: AgentContext) -> AgentResult: ...


class PendingActionStore:
    """Human-in-the-loop action store. Writes to Outlook/Veeva only happen on explicit confirm.

    Actions are bound to the creating user and expire after 15 minutes. Production backs this
    with Redis (TTL keys) so any replica can confirm.
    """

    def __init__(self) -> None:
        self._items: dict[str, PendingAction] = {}

    def put(self, action: PendingAction) -> None:
        self._purge()
        self._items[action.action_id] = action

    def take(self, action_id: str, owner_oid: str) -> PendingAction:
        self._purge()
        action = self._items.get(action_id)
        if action is None:
            raise NotFoundError("Action not found or expired")
        if action.owner_oid != owner_oid:
            raise AuthorizationError("Action belongs to another user")
        return self._items.pop(action_id)

    def _purge(self) -> None:
        now = datetime.now(UTC)
        for k in [k for k, v in self._items.items() if v.expires_at < now]:
            del self._items[k]

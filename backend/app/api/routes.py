"""HTTP transport layer. Thin: validation, auth dependencies, and delegation to the orchestrator."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.audit.store import AuditEvent, ChainVerification, StoredAuditEvent
from app.config import Settings, settings_dependency
from app.core.errors import AuthorizationError, NotFoundError
from app.core.logging import correlation_id_var
from app.orchestrator import ActionResult, TurnResult
from app.resilience.faults import FaultMode
from app.security.auth import DEMO_PERSONAS, Principal, Role, get_principal, issue_demo_token, require_roles
from app.services import Services, get_services

api = APIRouter(prefix="/api")
health = APIRouter()


# ----------------------------------------------------------------------------- schemas
class DemoTokenRequest(BaseModel):
    persona: str = Field(pattern="^(rep|manager|compliance)$")


class DemoTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    persona: str


class MeResponse(BaseModel):
    oid: str
    name: str
    email: str
    roles: list[str]
    territory: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class FaultRequest(BaseModel):
    dependency: str = Field(pattern="^(graph|crm|fabric)$")
    mode: FaultMode


class OpsStatus(BaseModel):
    environment: str
    version: str
    auth_mode: str
    llm: str
    kb_version: str
    kb_documents: list[dict]
    integrations: dict[str, str]  # dependency -> live | mock
    breakers: list[dict]
    faults: dict[str, str]
    fault_injection_allowed: bool
    answer_cache: dict[str, int]


def _orchestrator(request: Request):
    return request.app.state.orchestrator


# ----------------------------------------------------------------------------- auth
@api.post("/auth/demo-token", response_model=DemoTokenResponse, tags=["auth"])
async def demo_token(body: DemoTokenRequest, settings: Settings = Depends(settings_dependency)) -> DemoTokenResponse:
    return DemoTokenResponse(access_token=issue_demo_token(body.persona, settings), persona=body.persona)


@api.get("/auth/personas", tags=["auth"])
async def personas(settings: Settings = Depends(settings_dependency)) -> dict:
    return {
        "auth_mode": settings.auth_mode,
        "personas": {
            k: {"name": v["name"], "roles": [str(r) for r in v["roles"]], "territory": v["territory"]} for k, v in DEMO_PERSONAS.items()
        },
    }


@api.get("/me", response_model=MeResponse, tags=["auth"])
async def me(principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        oid=principal.oid,
        name=principal.name,
        email=principal.email,
        roles=sorted(str(r) for r in principal.roles),
        territory=principal.territory,
    )


# ----------------------------------------------------------------------------- chat
@api.post("/chat", response_model=TurnResult, tags=["chat"])
async def chat(body: ChatRequest, request: Request, principal: Principal = Depends(get_principal)) -> TurnResult:
    return await _orchestrator(request).handle_turn(
        principal, body.message, correlation_id=correlation_id_var.get(), conversation_id=body.conversation_id
    )


@api.post("/actions/{action_id}/confirm", response_model=ActionResult, tags=["chat"])
async def confirm(action_id: str, request: Request, principal: Principal = Depends(get_principal)) -> ActionResult:
    return await _orchestrator(request).confirm_action(principal, action_id, correlation_id=correlation_id_var.get())


@api.post("/actions/{action_id}/cancel", response_model=ActionResult, tags=["chat"])
async def cancel(action_id: str, request: Request, principal: Principal = Depends(get_principal)) -> ActionResult:
    return await _orchestrator(request).cancel_action(principal, action_id, correlation_id=correlation_id_var.get())


# ----------------------------------------------------------------------------- audit
@api.get("/audit", response_model=list[StoredAuditEvent], tags=["compliance"])
async def audit_list(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = Query(None, max_length=40),
    _: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
    services: Services = Depends(get_services),
) -> list[StoredAuditEvent]:
    return await services.audit.list(limit=limit, event_type=event_type)


@api.get("/audit/verify", response_model=ChainVerification, tags=["compliance"])
async def audit_verify(
    _: Principal = Depends(require_roles(Role.COMPLIANCE_OFFICER)),
    services: Services = Depends(get_services),
) -> ChainVerification:
    return await services.audit.verify()


# ----------------------------------------------------------------------------- ops
@api.get("/ops/status", response_model=OpsStatus, tags=["ops"])
async def ops_status(
    request: Request,
    _: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> OpsStatus:
    s = services.settings
    qa = request.app.state.orchestrator.agents["product_qa"]
    return OpsStatus(
        environment=s.environment,
        version=s.app_version,
        auth_mode=s.auth_mode,
        llm=f"{services.llm.name}:{s.azure_openai_deployment}" if services.llm.enabled else "offline (rules + extractive)",
        kb_version=services.knowledge.version,
        kb_documents=[
            {"doc_id": d.doc_id, "title": d.title, "mlr_id": d.mlr_id, "indexed": d.indexed, "exclusion_reason": d.exclusion_reason}
            for d in services.knowledge.documents
        ],
        integrations=services.integration_modes(),
        breakers=[b.snapshot() for b in services.breakers.values()],
        faults=services.faults.snapshot(),
        fault_injection_allowed=s.fault_injection_allowed,
        answer_cache={"hits": qa.cache.hits, "misses": qa.cache.misses},
    )


async def _audit_ops(services: Services, principal: Principal, event_type: str, payload: dict) -> None:
    await services.audit.append(
        AuditEvent(
            correlation_id=correlation_id_var.get(),
            actor_oid=principal.oid,
            actor_roles=sorted(str(r) for r in principal.roles),
            event_type=event_type,
            payload=payload,
        )
    )


@api.post("/ops/faults", tags=["ops"])
async def set_fault(
    body: FaultRequest,
    principal: Principal = Depends(require_roles(Role.DISTRICT_MANAGER, Role.COMPLIANCE_OFFICER)),
    services: Services = Depends(get_services),
) -> dict:
    if not services.settings.fault_injection_allowed:
        raise AuthorizationError("Fault injection is disabled in this environment")
    services.faults.set(body.dependency, body.mode)
    await _audit_ops(services, principal, "ops.fault_injected", body.model_dump(mode="json"))
    return {"faults": services.faults.snapshot()}


@api.post("/ops/breakers/{name}/reset", tags=["ops"])
async def reset_breaker(
    name: str,
    principal: Principal = Depends(require_roles(Role.DISTRICT_MANAGER, Role.COMPLIANCE_OFFICER)),
    services: Services = Depends(get_services),
) -> dict:
    breaker = services.breakers.get(name)
    if breaker is None:
        raise NotFoundError(f"No breaker named {name}")
    breaker.reset()
    await _audit_ops(services, principal, "ops.breaker_reset", {"breaker": name})
    return breaker.snapshot()


# ----------------------------------------------------------------------------- health (ACA probes)
@health.get("/health/live", tags=["health"])
async def live() -> dict:
    return {"status": "ok"}


@health.get("/health/ready", tags=["health"])
async def ready(services: Services = Depends(get_services)) -> JSONResponse:
    checks = {
        "audit_store": services.audit.ping(),
        "knowledge_index": len(services.knowledge.chunks) > 0,
    }
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)

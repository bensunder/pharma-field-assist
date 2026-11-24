"""Composition root: builds every long-lived dependency once per process.

Held on ``app.state.services`` and injected into routes via ``get_services``. Tests build their own
``Services`` with tmp paths, which is why nothing here is a module-level singleton.

Integration clients are chosen per ``PFA_*_MODE``. Live mode with incomplete configuration fails at
start-up (the container never becomes ready) instead of silently serving mock data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from fastapi import Request

from app.agents.base import PendingActionStore
from app.audit.store import AuditStore
from app.config import IntegrationMode, Settings
from app.integrations.crm import CrmClient, MockCrmClient, SalesforceCrmClient
from app.integrations.fabric import AnalyticsClient, FabricSqlClient, MockFabricClient
from app.integrations.graph_calendar import CalendarClient, GraphCalendarClient, MockCalendarClient
from app.knowledge.index import KnowledgeIndex
from app.llm.provider import LLMProvider, build_provider
from app.resilience.breaker import CircuitBreaker
from app.resilience.faults import FaultRegistry
from app.security.app_tokens import ClientCredentialsProvider
from app.security.obo import OboBroker


class ConfigurationError(RuntimeError):
    pass


@dataclass
class Services:
    settings: Settings
    audit: AuditStore
    knowledge: KnowledgeIndex
    llm: LLMProvider
    obo: OboBroker
    faults: FaultRegistry
    graph: CalendarClient
    crm: CrmClient
    fabric: AnalyticsClient
    actions: PendingActionStore
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    http: httpx.AsyncClient | None = None

    @classmethod
    def build(cls, settings: Settings, *, llm: LLMProvider | None = None, http: httpx.AsyncClient | None = None) -> Services:
        missing = settings.missing_live_config()
        if missing:
            raise ConfigurationError("Configuration invalid or missing: " + ", ".join(missing))

        faults = FaultRegistry()
        breakers = {
            n: CircuitBreaker(n, failure_threshold=settings.breaker_failure_threshold, reset_timeout=settings.breaker_reset_seconds)
            for n in ("graph", "crm", "fabric")
        }
        any_live = IntegrationMode.LIVE in (settings.graph_mode, settings.crm_mode, settings.fabric_mode)
        if http is None and any_live:
            http = httpx.AsyncClient(
                timeout=settings.integration_timeout_seconds,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                headers={"User-Agent": f"PharmaFieldAssist/{settings.app_version}"},
            )
        app_tokens = (
            ClientCredentialsProvider(settings.entra_tenant_id, settings.integration_client_id, settings.integration_client_secret, http)
            if http is not None and settings.integration_client_id and settings.integration_client_secret and settings.entra_tenant_id
            else None
        )

        graph: CalendarClient = (
            GraphCalendarClient(settings, breakers["graph"], faults, http, app_tokens)  # type: ignore[arg-type]
            if settings.graph_mode == IntegrationMode.LIVE
            else MockCalendarClient(breakers["graph"], faults)
        )
        crm: CrmClient = (
            SalesforceCrmClient(settings, breakers["crm"], faults, http)  # type: ignore[arg-type]
            if settings.crm_mode == IntegrationMode.LIVE
            else MockCrmClient(settings.crm_dataset_path, breakers["crm"], faults)
        )
        fabric: AnalyticsClient = (
            FabricSqlClient(settings, breakers["fabric"], faults, app_tokens)  # type: ignore[arg-type]
            if settings.fabric_mode == IntegrationMode.LIVE
            else MockFabricClient(settings.fabric_dataset_path, breakers["fabric"], faults)
        )
        return cls(
            settings=settings,
            audit=AuditStore(settings.audit_db_path),
            knowledge=KnowledgeIndex.from_directory(settings.knowledge_dir),
            llm=llm or build_provider(settings),
            obo=OboBroker(settings, http),
            faults=faults,
            graph=graph,
            crm=crm,
            fabric=fabric,
            actions=PendingActionStore(),
            breakers=breakers,
            http=http,
        )

    def integration_modes(self) -> dict[str, str]:
        return {"graph": self.graph.mode, "crm": self.crm.mode, "fabric": self.fabric.mode}

    async def aclose(self) -> None:
        await self.llm.aclose()
        if self.http is not None:
            await self.http.aclose()
        self.audit.close()


def get_services(request: Request) -> Services:
    return request.app.state.services

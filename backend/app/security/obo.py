"""On-Behalf-Of (OBO) token broker.

Downstream calls to Microsoft Graph and Veeva/Salesforce must run as the signed-in rep, never as
the platform's own identity — that is what keeps CRM row-level visibility and Outlook mailbox
scoping correct. The SPA's access token (audience = this API) is exchanged at the Entra token
endpoint for a token scoped to the downstream resource.

In demo mode we return a deterministic synthetic token that encodes the user + scope, so the trace
panel can show exactly which identity and scope each tool call used.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import AuthMode, Settings
from app.core.errors import DependencyUnavailableError
from app.security.auth import Principal

log = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/Calendars.ReadWrite"
CRM_SCOPE = "api://veeva-crm-connector/.default"


@dataclass(frozen=True)
class DelegatedToken:
    access_token: str
    scope: str
    subject_oid: str
    expires_at: float
    subject_upn: str = ""  # used to map the user to downstream identities (mailbox, Salesforce username)

    @property
    def fingerprint(self) -> str:
        """Safe-to-log token identifier (never log the token itself)."""
        return hashlib.sha256(self.access_token.encode()).hexdigest()[:12]


class OboBroker:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http
        # Cache keyed by (user, scope). Tokens are reused until 5 minutes before expiry.
        self._cache: dict[tuple[str, str], DelegatedToken] = {}

    async def acquire(self, principal: Principal, scope: str) -> DelegatedToken:
        key = (principal.oid, scope)
        cached = self._cache.get(key)
        if cached and cached.expires_at - 300 > time.time():
            return cached
        token = self._demo_token(principal, scope) if self._settings.auth_mode == AuthMode.DEMO else await self._exchange(principal, scope)
        self._cache[key] = token
        return token

    def _demo_token(self, principal: Principal, scope: str) -> DelegatedToken:
        digest = hashlib.sha256(f"{principal.oid}|{scope}".encode()).hexdigest()
        return DelegatedToken(
            access_token=f"demo-obo.{digest}",
            scope=scope,
            subject_oid=principal.oid,
            expires_at=time.time() + 3600,
            subject_upn=principal.email,
        )

    async def _exchange(self, principal: Principal, scope: str) -> DelegatedToken:
        s = self._settings
        if not (s.entra_tenant_id and s.entra_api_client_id and s.entra_api_client_secret):
            raise DependencyUnavailableError("OBO is not configured for this environment")
        url = f"https://login.microsoftonline.com/{s.entra_tenant_id}/oauth2/v2.0/token"
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": s.entra_api_client_id,
            "client_secret": s.entra_api_client_secret,
            "assertion": principal.raw_token,
            "scope": scope,
            "requested_token_use": "on_behalf_of",
        }
        client = self._http or httpx.AsyncClient(timeout=10)
        try:
            resp = await client.post(url, data=form)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError("Entra token endpoint unreachable") from exc
        finally:
            if self._http is None:
                await client.aclose()
        if resp.status_code != 200:
            # AADSTS65001 = consent required; surfaced so the SPA can trigger incremental consent.
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            log.warning("obo_exchange_failed", extra={"status": resp.status_code, "error": body.get("error")})
            raise DependencyUnavailableError("OBO exchange failed", details={"error": body.get("error"), "scope": scope})
        data = resp.json()
        return DelegatedToken(
            access_token=data["access_token"],
            scope=scope,
            subject_oid=principal.oid,
            expires_at=time.time() + int(data.get("expires_in", 3600)),
            subject_upn=principal.email,
        )

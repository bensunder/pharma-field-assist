"""App-only (client-credentials) token provider for Entra ID.

Used where there is no signed-in user token to exchange (demo personas -> Graph mailbox) and for
service-to-service data access (Fabric SQL endpoint). Tokens are cached per scope until five minutes
before expiry, and concurrent callers share one in-flight request so a burst of chat turns doesn't
stampede the token endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.errors import DependencyUnavailableError

log = logging.getLogger(__name__)

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
SQL_SCOPE = "https://database.windows.net/.default"


@dataclass(frozen=True)
class AppToken:
    access_token: str
    scope: str
    expires_at: float

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.access_token.encode()).hexdigest()[:12]


class ClientCredentialsProvider:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._cache: dict[str, AppToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, scope: str) -> AppToken:
        cached = self._cache.get(scope)
        if cached and cached.expires_at - 300 > time.time():
            return cached
        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            cached = self._cache.get(scope)  # another coroutine may have refreshed it
            if cached and cached.expires_at - 300 > time.time():
                return cached
            token = await self._fetch(scope)
            self._cache[scope] = token
            return token

    async def _fetch(self, scope: str) -> AppToken:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": scope,
        }
        try:
            resp = await self._http.post(self._url, data=form)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError("Entra token endpoint unreachable", details={"scope": scope}) from exc
        if resp.status_code != 200:
            error = _json(resp).get("error")
            log.warning("app_token_failed", extra={"status": resp.status_code, "error": error, "scope": scope})
            raise DependencyUnavailableError(
                "App token request failed (check client id/secret and tenant)", details={"error": error, "scope": scope}
            )
        data = resp.json()
        return AppToken(access_token=data["access_token"], scope=scope, expires_at=time.time() + int(data.get("expires_in", 3600)))


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}

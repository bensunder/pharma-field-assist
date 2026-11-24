"""CRM integration — Veeva CRM / Salesforce platform.

* ``SalesforceCrmClient`` — LIVE. Salesforce REST API:
    GET  /services/data/{v}/query?q=SOQL          accounts in the caller's territory
    POST /services/data/{v}/sobjects/Task         call logged against the account (WhatId)
  Auth, by ``PFA_SF_AUTH``:
    client_credentials — a connected-app integration user; territory is enforced by our SOQL filter.
    jwt_bearer         — a short-lived assertion signed with the connected app's key, ``sub`` = the
                         rep's Salesforce username, so Salesforce sharing rules apply to that rep
                         (the Salesforce equivalent of acting On-Behalf-Of the user).
* ``MockCrmClient`` — local JSON accounts for the offline demo.

``AccountMapper`` is the single place that knows field names. The defaults use only *standard*
Account fields so a fresh Developer Edition org works with zero metadata setup; a Veeva org swaps in
``Specialty_1_vod__c``, ``Call2_vod__c`` etc. here and nowhere else.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import ClassVar, Protocol

import httpx
import jwt

from app.config import SalesforceAuth, Settings
from app.core.errors import AuthorizationError, DependencyUnavailableError, NotFoundError
from app.integrations import http_errors
from app.resilience.breaker import CircuitBreaker
from app.resilience.faults import FaultRegistry
from app.security.obo import DelegatedToken


class CrmClient(Protocol):
    dependency: str
    breaker: CircuitBreaker
    mode: str

    def identity(self, token: DelegatedToken) -> str: ...
    async def list_accounts(self, token: DelegatedToken, territory: str) -> list[dict]: ...
    async def find_account(self, token: DelegatedToken, territory: str, query: str) -> dict | None: ...
    async def log_call(self, token: DelegatedToken, territory: str, account_id: str, notes: str, products: list[str]) -> dict: ...


def match_account(accounts: list[dict], query: str) -> dict | None:
    """Resolve "Dr. Patel" / "Memorial Heart" style references against visible accounts."""
    q = query.lower()
    words = set(re.split(r"[^a-z'-]+", q))
    for a in accounts:
        if a["name"].split()[-1].lower() in words:
            return a
    for a in accounts:
        if a["organization"] and a["organization"].lower() in q:
            return a
    return None


def territory_visible(account_territory: str, scope: str) -> bool:
    # "US" (home office) sees all; a district ("TX-HOU") sees its territories; a rep sees one.
    return scope == "US" or account_territory.startswith(scope)


# ============================================================================== mapping
class AccountMapper:
    """Standard-field mapping for a Salesforce org (Developer Edition friendly).

    Account.Site           -> territory code (TX-HOU-03)
    Account.AccountNumber  -> external account id (ACC-1001)
    Account.Rating         -> segment (Hot=A, Warm=B, Cold=C)
    Account.Description    -> "specialty=...;org=...;products=a,b"
    LastActivityDate       -> last call
    """

    FIELDS: ClassVar[str] = "Id, Name, Site, AccountNumber, Rating, Description, LastActivityDate"
    TERRITORY_FIELD: ClassVar[str] = "Site"
    RATING_TO_SEGMENT: ClassVar[dict[str, str]] = {"Hot": "A", "Warm": "B", "Cold": "C"}
    SEGMENT_TO_RATING: ClassVar[dict[str, str]] = {"A": "Hot", "B": "Warm", "C": "Cold"}

    @classmethod
    def to_account(cls, rec: dict) -> dict:
        meta = dict(part.split("=", 1) for part in (rec.get("Description") or "").split(";") if "=" in part)
        return {
            "account_id": rec["Id"],
            "external_id": rec.get("AccountNumber"),
            "name": rec["Name"],
            "specialty": meta.get("specialty", ""),
            "organization": meta.get("org", ""),
            "territory": rec.get("Site") or "",
            "segment": cls.RATING_TO_SEGMENT.get(rec.get("Rating") or "", "C"),
            "products": [p for p in meta.get("products", "").split(",") if p],
            "last_call": rec.get("LastActivityDate"),
        }

    @classmethod
    def to_record(cls, account: dict) -> dict:
        return {
            "Name": account["name"],
            "Site": account["territory"],
            "AccountNumber": account["account_id"],
            "Rating": cls.SEGMENT_TO_RATING.get(account["segment"], "Cold"),
            "Description": f"specialty={account['specialty']};org={account['organization']};products={','.join(account['products'])}",
        }


def soql_literal(value: str, *, like: bool = False) -> str:
    """Escape a value for a SOQL string literal (and LIKE wildcards when used in LIKE)."""
    v = value.replace("\\", "\\\\").replace("'", "\\'")
    if like:
        v = v.replace("%", "\\%").replace("_", "\\_")
    return v


# ============================================================================== live
class SalesforceCrmClient:
    dependency = "crm"
    mode = "live"

    def __init__(self, settings: Settings, breaker: CircuitBreaker, faults: FaultRegistry, http: httpx.AsyncClient) -> None:
        self._s = settings
        self.breaker = breaker
        self._faults = faults
        self._http = http
        self._sessions: dict[str, tuple[str, str, float]] = {}  # key -> (access_token, instance_url, expires_at)
        self._lock = asyncio.Lock()
        self._key: str | None = None
        if settings.sf_auth == SalesforceAuth.JWT_BEARER and settings.sf_jwt_private_key_path:
            self._key = Path(settings.sf_jwt_private_key_path).read_text(encoding="utf-8")

    # -- identity / auth ------------------------------------------------------------
    def _username(self, token: DelegatedToken) -> str:
        return self._s.sf_username_map.get(token.subject_upn) or token.subject_upn

    def identity(self, token: DelegatedToken) -> str:
        if self._s.sf_auth == SalesforceAuth.JWT_BEARER:
            return f"user:{self._username(token)} (Salesforce JWT bearer)"
        return "app:salesforce-integration-user (client credentials)"

    async def _session(self, token: DelegatedToken, *, force: bool = False) -> tuple[str, str]:
        jwt_mode = self._s.sf_auth == SalesforceAuth.JWT_BEARER
        key = self._username(token) if jwt_mode else "__integration__"
        async with self._lock:
            cached = self._sessions.get(key)
            if cached and not force and cached[2] > time.time():
                return cached[0], cached[1]
            login = self._s.sf_login_url.rstrip("/")
            if jwt_mode:
                assertion = jwt.encode(
                    {"iss": self._s.sf_client_id, "sub": key, "aud": login, "exp": int(time.time()) + 180},
                    self._key,
                    algorithm="RS256",
                )
                form = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
            else:
                form = {
                    "grant_type": "client_credentials",
                    "client_id": self._s.sf_client_id,
                    "client_secret": self._s.sf_client_secret,
                }
            try:
                resp = await http_errors.send("crm", lambda: self._http.post(f"{login}/services/oauth2/token", data=form))
            except DependencyUnavailableError as exc:
                raise DependencyUnavailableError(
                    "Salesforce login failed (check connected app, user pre-authorization, My Domain URL)",
                    details=exc.details,
                ) from exc
            data = resp.json()
            # Salesforce doesn't return expires_in; refresh well inside the default 2h session timeout.
            self._sessions[key] = (data["access_token"], data["instance_url"], time.time() + 1800)
            return data["access_token"], data["instance_url"]

    async def _request(self, token: DelegatedToken, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in (0, 1):
            access, instance = await self._session(token, force=attempt == 1)
            url = f"{instance}/services/data/{self._s.sf_api_version}{path}"
            try:
                return await http_errors.send(
                    "crm",
                    lambda u=url, a=access: self._http.request(method, u, headers={"Authorization": f"Bearer {a}"}, **kwargs),
                )
            except DependencyUnavailableError as exc:
                if exc.details.get("status") == 401 and attempt == 0:
                    continue  # session expired/revoked: re-login once
                raise
        raise DependencyUnavailableError("Salesforce session could not be established")  # pragma: no cover

    async def _query(self, token: DelegatedToken, soql: str) -> list[dict]:
        records: list[dict] = []
        resp = await self._request(token, "GET", "/query", params={"q": soql})
        body = resp.json()
        records.extend(body.get("records", []))
        pages = 0
        while not body.get("done", True) and body.get("nextRecordsUrl") and pages < 5:
            access, instance = await self._session(token)
            url = f"{instance}{body['nextRecordsUrl']}"
            nxt = await http_errors.send("crm", lambda u=url, a=access: self._http.get(u, headers={"Authorization": f"Bearer {a}"}))
            body = nxt.json()
            records.extend(body.get("records", []))
            pages += 1
        return records

    def _territory_clause(self, territory: str) -> str:
        if territory == "US":
            return ""
        return f" WHERE {AccountMapper.TERRITORY_FIELD} LIKE '{soql_literal(territory, like=True)}%'"

    # -- API ------------------------------------------------------------------------
    async def list_accounts(self, token: DelegatedToken, territory: str) -> list[dict]:
        # values are escaped by soql_literal(); field names are constants
        soql = f"SELECT {AccountMapper.FIELDS} FROM Account{self._territory_clause(territory)} ORDER BY Rating, Name LIMIT 200"  # noqa: S608

        async def _op() -> list[dict]:
            self._faults.maybe_raise(self.dependency)
            accounts = [AccountMapper.to_account(r) for r in await self._query(token, soql)]
            # defence in depth: never return an account outside the caller's scope
            return [a for a in accounts if territory_visible(a["territory"], territory)]

        return await self.breaker.call(_op)

    async def find_account(self, token: DelegatedToken, territory: str, query: str) -> dict | None:
        return match_account(await self.list_accounts(token, territory), query)

    async def log_call(self, token: DelegatedToken, territory: str, account_id: str, notes: str, products: list[str]) -> dict:
        if not account_id.isalnum() or len(account_id) not in (15, 18):
            raise NotFoundError("Invalid Salesforce account id")

        async def _op() -> dict:
            self._faults.maybe_raise(self.dependency)
            # account_id validated as a 15/18-char alphanumeric Salesforce Id above
            rows = await self._query(token, f"SELECT {AccountMapper.FIELDS} FROM Account WHERE Id = '{account_id}' LIMIT 1")  # noqa: S608
            if not rows:
                raise NotFoundError("Account not found or not visible to you")
            account = AccountMapper.to_account(rows[0])
            if not territory_visible(account["territory"], territory):
                raise AuthorizationError("Account is outside your territory")
            task = {
                "Subject": f"Call — {account['name']}",
                "WhatId": account_id,
                "Description": f"{notes}\n\nProducts discussed: {', '.join(products)}\nLogged via PharmaFieldAssist (human-confirmed).",
                "Status": "Completed",
                "Priority": "Normal",
                "ActivityDate": date.today().isoformat(),
            }
            resp = await self._request(token, "POST", "/sobjects/Task", json=task)
            return {
                "call_id": resp.json().get("id"),
                "account_id": account_id,
                "owner_oid": token.subject_oid,
                "notes": notes,
                "products_discussed": products,
                "status": "Completed",
                "call_datetime": datetime.now(UTC).isoformat(),
                "system": "salesforce",
            }

        # Not retried: a Task create isn't idempotent.
        return await self.breaker.call(_op, retries=0)


# ============================================================================== mock
class MockCrmClient:
    dependency = "crm"
    mode = "mock"

    def __init__(self, dataset: Path, breaker: CircuitBreaker, faults: FaultRegistry) -> None:
        self.breaker = breaker
        self._faults = faults
        self._accounts: list[dict] = json.loads(dataset.read_text(encoding="utf-8"))["accounts"]
        self._calls: list[dict] = []

    def identity(self, token: DelegatedToken) -> str:
        return f"user:{token.subject_oid} (OBO, mock Veeva)"

    async def list_accounts(self, token: DelegatedToken, territory: str) -> list[dict]:
        async def _op() -> list[dict]:
            self._faults.maybe_raise(self.dependency)
            return [a for a in self._accounts if territory_visible(a["territory"], territory)]

        return await self.breaker.call(_op)

    async def find_account(self, token: DelegatedToken, territory: str, query: str) -> dict | None:
        return match_account(await self.list_accounts(token, territory), query)

    async def log_call(self, token: DelegatedToken, territory: str, account_id: str, notes: str, products: list[str]) -> dict:
        async def _op() -> dict:
            self._faults.maybe_raise(self.dependency)
            account = next((a for a in self._accounts if a["account_id"] == account_id), None)
            if account is None:
                raise NotFoundError("Account not found")
            if not territory_visible(account["territory"], territory):
                raise AuthorizationError("Account is outside your territory")
            record = {
                "call_id": f"a0X{uuid.uuid4().hex[:12]}",
                "account_id": account_id,
                "owner_oid": token.subject_oid,
                "notes": notes,
                "products_discussed": products,
                "status": "Submitted_vod",
                "call_datetime": datetime.now(UTC).isoformat(),
                "system": "mock",
            }
            self._calls.append(record)
            account["last_call"] = record["call_datetime"]
            return record

        return await self.breaker.call(_op, retries=0)

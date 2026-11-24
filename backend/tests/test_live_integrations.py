"""Live integration clients (Graph, Salesforce, Fabric) — exercised against recorded-shape HTTP fakes.

No network: every HTTP call goes through httpx.MockTransport, Fabric through an injected connection
factory. These tests pin the exact wire contract (URLs, headers, bodies, auth grants) and the error
semantics (what trips the breaker, what fails fast).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import AuthMode, IntegrationMode, SalesforceAuth, Settings
from app.core.errors import AuthorizationError, DependencyUnavailableError, NotFoundError
from app.integrations.crm import AccountMapper, SalesforceCrmClient, soql_literal
from app.integrations.fabric import FabricSqlClient, build_weekly_query
from app.integrations.graph_calendar import CENTRAL, GraphCalendarClient
from app.main import create_app
from app.resilience.breaker import BreakerState, CircuitBreaker, TransientError
from app.resilience.faults import FaultRegistry
from app.security.app_tokens import ClientCredentialsProvider
from app.security.obo import DelegatedToken
from app.services import ConfigurationError, Services
from tests.conftest import auth

TENANT = "11111111-2222-3333-4444-555555555555"
MAILBOX = "jordan.reyes@contoso-demo.onmicrosoft.com"
SF = "https://contoso-dev-ed.develop.my.salesforce.com"
FUTURE = (datetime.now(CENTRAL) + timedelta(days=10)).date()
while FUTURE.weekday() >= 5:
    FUTURE += timedelta(days=1)

REP = DelegatedToken(
    access_token="user-delegated-token",
    scope="x",
    subject_oid="oid-rep",
    expires_at=time.time() + 3600,
    subject_upn="jordan.reyes@contoso-pharma.com",
)


class FakeHttp:
    """Route table for httpx.MockTransport; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: list[tuple[str, str, object]] = []

    def on(self, method: str, url_part: str, response) -> FakeHttp:
        self.routes.append((method, url_part, response))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, part, response in self.routes:
            if request.method == method and part in str(request.url):
                return response(request) if callable(response) else response
        return httpx.Response(599, json={"error": f"unrouted {request.method} {request.url}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def find(self, method: str, url_part: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and url_part in str(r.url)]


def token_ok(_req=None) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "app-token", "expires_in": 3599})


def settings(tmp_path, **kw) -> Settings:
    base = dict(
        audit_db_path=tmp_path / "a.db",
        entra_tenant_id=TENANT,
        integration_client_id="int-app",
        integration_client_secret="s",
        graph_demo_mailbox=MAILBOX,
        sf_login_url=SF,
        sf_client_id="sf-id",
        sf_client_secret="sf-secret",
        fabric_sql_endpoint="abc.datawarehouse.fabric.microsoft.com",
        fabric_database="pfa_lakehouse",
    )
    base.update(kw)
    return Settings(**base)


# ============================================================================== Graph
def _graph_event(subject: str, h1: int, h2: int, **extra) -> dict:
    d = FUTURE.isoformat()
    return {
        "id": f"AAMk{subject}",
        "subject": subject,
        "showAs": extra.get("showAs", "busy"),
        "isCancelled": extra.get("isCancelled", False),
        "start": {"dateTime": f"{d}T{h1:02d}:00:00.0000000", "timeZone": "Central Standard Time"},
        "end": {"dateTime": f"{d}T{h2:02d}:00:00.0000000", "timeZone": "Central Standard Time"},
    }


def _graph(tmp_path, fake: FakeHttp, **kw) -> GraphCalendarClient:
    s = settings(tmp_path, graph_mode=IntegrationMode.LIVE, **kw)
    http = fake.client()
    tokens = ClientCredentialsProvider(TENANT, "int-app", "s", http)
    return GraphCalendarClient(s, CircuitBreaker("graph", failure_threshold=2), FaultRegistry(), http, tokens)


async def test_graph_app_only_calendarview_contract_and_pagination(tmp_path):
    page2 = "https://graph.microsoft.com/v1.0/users/x/calendarView?$skiptoken=2"
    fake = (
        FakeHttp()
        .on("POST", "login.microsoftonline.com", token_ok)
        .on("GET", "skiptoken=2", httpx.Response(200, json={"value": [_graph_event("Lunch", 12, 13)]}))
        .on(
            "GET",
            "/calendarView",
            httpx.Response(
                200,
                json={
                    "value": [
                        _graph_event("Huddle", 8, 9),
                        _graph_event("Tentative-free", 9, 10, showAs="free"),
                        _graph_event("Cancelled", 10, 11, isCancelled=True),
                    ],
                    "@odata.nextLink": page2,
                },
            ),
        )
    )
    client = _graph(tmp_path, fake)
    busy = await client.get_busy(REP, FUTURE)
    assert [e.subject for e in busy] == ["Huddle", "Lunch"]  # free + cancelled excluded, page 2 followed
    assert busy[0].start == datetime.combine(FUTURE, datetime.min.time().replace(hour=8), CENTRAL)

    first = fake.find("GET", "/calendarView")[0]
    assert f"/v1.0/users/{MAILBOX}/calendarView" in str(first.url)
    assert first.headers["Authorization"] == "Bearer app-token"
    assert first.headers["Prefer"] == 'outlook.timezone="Central Standard Time"'
    assert {"startDateTime", "endDateTime", "$select"} <= set(first.url.params.keys())
    form = parse_qs(fake.find("POST", "login.microsoftonline.com")[0].content.decode())
    assert form["grant_type"] == ["client_credentials"] and form["scope"] == ["https://graph.microsoft.com/.default"]

    slots = await client.find_free_slots(REP, FUTURE, limit=3)
    assert [s.hour for s in slots] == [9, 9, 10] and slots[1].minute == 30
    assert len(fake.find("POST", "login.microsoftonline.com")) == 1  # app token cached


async def test_graph_create_event_is_idempotent_and_retried_once(tmp_path):
    calls = {"n": 0}

    def create(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable", "message": "busy"}})
        return httpx.Response(201, json={"id": "AAMkNEW", "subject": json.loads(req.content)["subject"]})

    fake = FakeHttp().on("POST", "login.microsoftonline.com", token_ok).on("POST", "/events", create)
    client = _graph(tmp_path, fake)
    start = datetime.combine(FUTURE, datetime.min.time().replace(hour=14), CENTRAL)
    ev = await client.create_event(
        REP,
        subject="HCP meeting — Dr. Anita Patel",
        start=start,
        duration=timedelta(minutes=30),
        attendees=("Dr. Anita Patel",),
        location="Memorial Heart",
        transaction_id="act_123",
    )
    assert ev.event_id == "AAMkNEW"
    posts = fake.find("POST", "/events")
    bodies = [json.loads(p.content) for p in posts]
    assert len(posts) == 2 and {b["transactionId"] for b in bodies} == {"act_123"}
    assert bodies[0]["start"] == {"dateTime": f"{FUTURE.isoformat()}T14:00:00", "timeZone": "Central Standard Time"}
    assert bodies[0]["end"]["dateTime"].endswith("T14:30:00")


async def test_graph_permission_error_fails_fast_without_tripping_breaker(tmp_path):
    fake = (
        FakeHttp()
        .on("POST", "login.microsoftonline.com", token_ok)
        .on("GET", "/calendarView", httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}))
    )
    client = _graph(tmp_path, fake)
    for _ in range(3):
        with pytest.raises(DependencyUnavailableError) as exc:
            await client.get_busy(REP, FUTURE)
    assert "permission denied" in exc.value.message and "ErrorAccessDenied" in exc.value.details["error"]
    assert client.breaker.state == BreakerState.CLOSED and len(fake.find("GET", "/calendarView")) == 3  # no retries


async def test_graph_throttling_trips_breaker(tmp_path):
    fake = FakeHttp().on("POST", "login.microsoftonline.com", token_ok).on("GET", "/calendarView", httpx.Response(429, json={}))
    client = _graph(tmp_path, fake)
    for _ in range(2):
        with pytest.raises(DependencyUnavailableError):
            await client.get_busy(REP, FUTURE)
    assert client.breaker.state == BreakerState.OPEN


async def test_graph_delegated_mode_uses_me_and_user_token(tmp_path):
    fake = FakeHttp().on("GET", "/me/calendarView", httpx.Response(200, json={"value": []}))
    client = _graph(tmp_path, fake, auth_mode=AuthMode.ENTRA, entra_api_client_id="api", entra_api_client_secret="x")
    assert await client.get_busy(REP, FUTURE) == []
    req = fake.requests[0]
    assert req.headers["Authorization"] == "Bearer user-delegated-token"
    assert not fake.find("POST", "login.microsoftonline.com")  # no app token in delegated mode
    assert "OBO" in client.identity(REP)


async def test_graph_mailbox_map_overrides_default(tmp_path):
    fake = FakeHttp().on("POST", "login", token_ok).on("GET", "/calendarView", httpx.Response(200, json={"value": []}))
    client = _graph(tmp_path, fake, graph_mailbox_map={"oid-rep": "rep2@contoso.onmicrosoft.com"})
    await client.get_busy(REP, FUTURE)
    assert "/users/rep2@contoso.onmicrosoft.com/" in str(fake.find("GET", "/calendarView")[0].url)


# ============================================================================== Salesforce
SF_ACCOUNTS = [
    {
        "Id": "001A0000001",
        "Name": "Dr. Anita Patel",
        "Site": "TX-HOU-03",
        "AccountNumber": "ACC-1001",
        "Rating": "Hot",
        "Description": "specialty=Cardiology;org=Memorial Heart Associates;products=cardivex",
        "LastActivityDate": None,
    },
    {
        "Id": "001A0000002",
        "Name": "Dr. Olivia Grant",
        "Site": "TX-DAL-01",
        "AccountNumber": "ACC-3001",
        "Rating": "Warm",
        "Description": "specialty=Endocrinology;org=Uptown;products=cardivex",
        "LastActivityDate": "2026-11-01",
    },
]
SF_ID_1, SF_ID_2 = "001A0000001AAAA", "001A0000002AAAA"
SF_ACCOUNTS[0]["Id"], SF_ACCOUNTS[1]["Id"] = SF_ID_1, SF_ID_2


def sf_token(_req=None):
    return httpx.Response(200, json={"access_token": "sf-session", "instance_url": SF, "token_type": "Bearer"})


def _sf(tmp_path, fake: FakeHttp, **kw) -> SalesforceCrmClient:
    s = settings(tmp_path, crm_mode=IntegrationMode.LIVE, **kw)
    return SalesforceCrmClient(s, CircuitBreaker("crm"), FaultRegistry(), fake.client())


async def test_salesforce_client_credentials_and_territory_soql(tmp_path):
    fake = (
        FakeHttp()
        .on("POST", "/services/oauth2/token", sf_token)
        .on("GET", "/query", httpx.Response(200, json={"done": True, "records": SF_ACCOUNTS}))  # server returns too much
    )
    client = _sf(tmp_path, fake)
    accounts = await client.list_accounts(REP, "TX-HOU-03")
    assert [a["name"] for a in accounts] == ["Dr. Anita Patel"]  # defence in depth filters TX-DAL
    a = accounts[0]
    assert (a["specialty"], a["organization"], a["segment"], a["products"]) == (
        "Cardiology",
        "Memorial Heart Associates",
        "A",
        ["cardivex"],
    )

    login = parse_qs(fake.find("POST", "/services/oauth2/token")[0].content.decode())
    assert login["grant_type"] == ["client_credentials"] and login["client_id"] == ["sf-id"]
    q = fake.find("GET", "/query")[0]
    assert "/services/data/v62.0/query" in str(q.url)
    assert "WHERE Site LIKE 'TX-HOU-03%'" in q.url.params["q"]
    assert q.headers["Authorization"] == "Bearer sf-session"
    assert "integration-user" in client.identity(REP)


@pytest.mark.parametrize(
    ("raw", "like", "expected"),
    [
        ("O'Brien", False, "O\\'Brien"),
        ("TX_%", True, "TX\\_\\%"),
        ("a\\b", False, "a\\\\b"),
        ("x' OR Name LIKE '", True, "x\\' OR Name LIKE \\'"),
    ],
)
def test_soql_literal_escaping(raw, like, expected):
    assert soql_literal(raw, like=like) == expected


async def test_salesforce_relogs_in_once_on_expired_session(tmp_path):
    state = {"n": 0}

    def query(_req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])
        return httpx.Response(200, json={"done": True, "records": SF_ACCOUNTS[:1]})

    fake = FakeHttp().on("POST", "/services/oauth2/token", sf_token).on("GET", "/query", query)
    client = _sf(tmp_path, fake)
    assert len(await client.list_accounts(REP, "TX-HOU-03")) == 1
    assert len(fake.find("POST", "/services/oauth2/token")) == 2


async def test_salesforce_log_call_creates_task(tmp_path):
    fake = (
        FakeHttp()
        .on("POST", "/services/oauth2/token", sf_token)
        .on("GET", "/query", httpx.Response(200, json={"done": True, "records": SF_ACCOUNTS[:1]}))
        .on("POST", "/sobjects/Task", httpx.Response(201, json={"id": "00TA000000NEW", "success": True}))
    )
    client = _sf(tmp_path, fake)
    rec = await client.log_call(REP, "TX-HOU-03", SF_ID_1, "Discussed formulary coverage", ["cardivex"])
    assert rec["call_id"] == "00TA000000NEW" and rec["system"] == "salesforce"
    task = json.loads(fake.find("POST", "/sobjects/Task")[0].content)
    assert task["WhatId"] == SF_ID_1 and task["Status"] == "Completed" and "cardivex" in task["Description"]
    assert f"Id = '{SF_ID_1}'" in fake.find("GET", "/query")[0].url.params["q"]


async def test_salesforce_log_call_guards(tmp_path):
    fake = (
        FakeHttp()
        .on("POST", "/services/oauth2/token", sf_token)
        .on("GET", "/query", httpx.Response(200, json={"done": True, "records": SF_ACCOUNTS[1:]}))
    )
    client = _sf(tmp_path, fake)
    with pytest.raises(NotFoundError):
        await client.log_call(REP, "TX-HOU-03", "001' OR Id != '", "n", [])  # injection attempt never reaches SOQL
    with pytest.raises(AuthorizationError):
        await client.log_call(REP, "TX-HOU-03", SF_ID_2, "notes", [])  # TX-DAL account
    assert not fake.find("POST", "/sobjects/Task")


async def test_salesforce_jwt_bearer_acts_as_mapped_user(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "sf.key"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    fake = (
        FakeHttp()
        .on("POST", "/services/oauth2/token", sf_token)
        .on("GET", "/query", httpx.Response(200, json={"done": True, "records": []}))
    )
    client = _sf(
        tmp_path,
        fake,
        sf_auth=SalesforceAuth.JWT_BEARER,
        sf_login_url="https://login.salesforce.com",
        sf_jwt_private_key_path=pem,
        sf_username_map={"jordan.reyes@contoso-pharma.com": "jreyes@contoso-dev.org"},
    )
    await client.list_accounts(REP, "TX-HOU-03")
    form = parse_qs(fake.find("POST", "/services/oauth2/token")[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(form["assertion"][0], key.public_key(), algorithms=["RS256"], audience="https://login.salesforce.com")
    assert claims["iss"] == "sf-id" and claims["sub"] == "jreyes@contoso-dev.org" and claims["exp"] - time.time() <= 180
    assert "jreyes@contoso-dev.org" in client.identity(REP)


def test_account_mapper_roundtrip():
    acct = {
        "account_id": "ACC-1",
        "name": "Dr. X Y",
        "specialty": "Cardiology",
        "organization": "Org",
        "territory": "TX-HOU-03",
        "segment": "B",
        "products": ["cardivex", "respira"],
    }
    rec = AccountMapper.to_record(acct)
    back = AccountMapper.to_account({**rec, "Id": "001x"})
    assert (back["segment"], back["products"], back["territory"]) == ("B", ["cardivex", "respira"], "TX-HOU-03")


# ============================================================================== Fabric
class FakeCursor:
    def __init__(self, conn) -> None:
        self.conn = conn

    def execute(self, sql, params):
        self.conn.executed.append((sql, params))
        if self.conn.raise_exc:
            raise self.conn.raise_exc

    def fetchall(self):
        return self.conn.rows

    def close(self):
        self.conn.cursor_closed = True


class FakeConn:
    def __init__(self, rows=None, raise_exc=None) -> None:
        self.rows, self.raise_exc = rows or [], raise_exc
        self.executed: list = []
        self.closed = self.cursor_closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class OperationalError(Exception):
    pass


class ProgrammingError(Exception):
    pass


def _fabric(tmp_path, conn: FakeConn, seen: dict) -> FabricSqlClient:
    s = settings(tmp_path, fabric_mode=IntegrationMode.LIVE)
    fake = FakeHttp().on("POST", "login.microsoftonline.com", token_ok)
    tokens = ClientCredentialsProvider(TENANT, "int-app", "s", fake.client())

    def connect(conn_str, token, timeout):
        seen.update(conn_str=conn_str, token=token, timeout=timeout, http=fake)
        return conn

    return FabricSqlClient(s, CircuitBreaker("fabric", failure_threshold=1), FaultRegistry(), tokens, connect=connect)


async def test_fabric_query_is_parameterised_and_scoped(tmp_path):
    conn = FakeConn(rows=[("2026-11-16", "TX-HOU-03", 180.0), ("2026-11-23", "TX-HOU-03", 190.0)])
    seen: dict = {}
    client = _fabric(tmp_path, conn, seen)
    series = await client.weekly_series(metric="trx", product="cardivex", territory_prefix="TX-HOU-03", weeks=2)
    assert series == {"TX-HOU-03": [{"week_start": "2026-11-16", "value": 180.0}, {"week_start": "2026-11-23", "value": 190.0}]}
    sql, params = conn.executed[0]
    assert "[trx]" in sql and "?" in sql and "cardivex" not in sql and "TX-HOU" not in sql
    assert params == (2, "cardivex", "cardivex", "TX-HOU-03", "TX-HOU-03%")
    assert "abc.datawarehouse.fabric.microsoft.com,1433" in seen["conn_str"] and "Encrypt=yes" in seen["conn_str"]
    assert seen["token"] == "app-token" and conn.closed and conn.cursor_closed
    form = parse_qs(seen["http"].find("POST", "login")[0].content.decode())
    assert form["scope"] == ["https://database.windows.net/.default"]


@pytest.mark.parametrize(("metric", "table"), [("trx; DROP TABLE x", "weekly"), ("trx", "weekly; DROP"), ("revenue", "weekly")])
def test_fabric_query_builder_rejects_untrusted_identifiers(metric, table):
    with pytest.raises(ValueError):
        build_weekly_query(table, metric)


def test_fabric_like_escaping_in_params(tmp_path):
    from app.integrations.fabric import _like_prefix

    assert _like_prefix("TX_HOU%") == "TX\\_HOU\\%%"


async def test_fabric_transient_errors_trip_breaker_and_permanent_fail_fast(tmp_path):
    client = _fabric(tmp_path, FakeConn(raise_exc=ProgrammingError("Invalid object name 'dbo.weekly'")), {})
    with pytest.raises(DependencyUnavailableError) as exc:
        await client.weekly_series(metric="trx", product="cardivex", territory_prefix="US", weeks=4)
    assert "query rejected" in exc.value.message and client.breaker.state == BreakerState.CLOSED

    client = _fabric(tmp_path, FakeConn(raise_exc=OperationalError("Login timeout expired")), {})
    with pytest.raises(DependencyUnavailableError):
        await client.weekly_series(metric="trx", product="cardivex", territory_prefix="US", weeks=4)
    assert client.breaker.state == BreakerState.OPEN


def test_transient_classifier():
    from app.integrations.fabric import _is_transient

    assert (
        _is_transient(OperationalError("x")) and _is_transient(Exception("Connection reset")) and not _is_transient(ProgrammingError("bad"))
    )
    assert issubclass(TransientError, Exception)


# ============================================================================== wiring / fail-fast
@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"graph_mode": "live", "graph_demo_mailbox": None}, "PFA_GRAPH_DEMO_MAILBOX"),
        ({"graph_mode": "live", "integration_client_secret": None}, "PFA_INTEGRATION_CLIENT_SECRET"),
        ({"crm_mode": "live", "sf_login_url": "https://login.salesforce.com"}, "My Domain"),
        ({"crm_mode": "live", "sf_auth": "jwt_bearer"}, "PFA_SF_JWT_PRIVATE_KEY_PATH"),
        ({"fabric_mode": "live", "fabric_database": None}, "PFA_FABRIC_DATABASE"),
    ],
)
def test_live_mode_fails_fast_on_missing_config(tmp_path, overrides, missing):
    with pytest.raises(ConfigurationError, match=missing):
        Services.build(settings(tmp_path, **overrides))


def test_end_to_end_chat_with_all_integrations_live(tmp_path):
    """Full API path (guardrails -> agents -> live clients -> HITL -> audit) with fake upstreams."""
    fake = (
        FakeHttp()
        .on("POST", "login.microsoftonline.com", token_ok)
        .on("GET", "/calendarView", httpx.Response(200, json={"value": [_graph_event("Huddle", 8, 9)]}))
        .on("POST", "/events", httpx.Response(201, json={"id": "AAMkLIVE"}))
        .on("POST", "/services/oauth2/token", sf_token)
        .on("GET", "/query", httpx.Response(200, json={"done": True, "records": SF_ACCOUNTS[:1]}))
        .on("POST", "/sobjects/Task", httpx.Response(201, json={"id": "00TLIVE", "success": True}))
    )
    s = settings(tmp_path, graph_mode=IntegrationMode.LIVE, crm_mode=IntegrationMode.LIVE, log_level="WARNING")
    app = create_app(s, Services.build(s, http=fake.client()))
    with TestClient(app) as c:
        h = auth(c, "rep")
        ops = c.get("/api/ops/status", headers=h).json()
        assert ops["integrations"] == {"graph": "live", "crm": "live", "fabric": "mock"}

        t = c.post("/api/chat", json={"message": f"Schedule a meeting with Dr. Patel on {FUTURE.isoformat()} at 2pm"}, headers=h).json()
        assert "Dr. Anita Patel" in t["pending_action"]["summary"]
        idents = {tc["tool"]: tc["identity"] for tc in t["tool_calls"]}
        assert idents["graph.calendarView"].startswith("app:client-credentials → mailbox")
        assert idents["crm.findAccount"].startswith("app:salesforce-integration-user")
        r = c.post(f"/api/actions/{t['pending_action']['action_id']}/confirm", headers=h).json()
        assert r["status"] == "completed" and r["record"]["event_id"] == "AAMkLIVE"
        assert json.loads(fake.find("POST", "/events")[0].content)["transactionId"] == t["pending_action"]["action_id"]

        t = c.post("/api/chat", json={"message": "log a call with Dr. Patel: discussed Cardivex savings card"}, headers=h).json()
        r = c.post(f"/api/actions/{t['pending_action']['action_id']}/confirm", headers=h).json()
        assert r["record"]["call_id"] == "00TLIVE"

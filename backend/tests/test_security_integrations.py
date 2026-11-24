"""Entra ID validation, OBO exchange and Azure OpenAI adapter — network mocked with httpx.MockTransport."""

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import AuthMode, Settings
from app.core.errors import AuthenticationError, AuthorizationError, DependencyUnavailableError
from app.llm.provider import AzureOpenAIProvider, parse_json
from app.security import auth as auth_mod
from app.security.auth import Principal, Role, principal_from_token
from app.security.obo import GRAPH_SCOPE, OboBroker

TENANT, AUD = "11111111-2222-3333-4444-555555555555", "api://pharma-field-assist"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def entra_settings(tmp_path) -> Settings:
    return Settings(
        auth_mode=AuthMode.ENTRA,
        entra_tenant_id=TENANT,
        entra_audience=AUD,
        entra_api_client_id="api-client",
        entra_api_client_secret="s3cret",
        audit_db_path=tmp_path / "a.db",
    )


@pytest.fixture
def jwks(monkeypatch, rsa_key):
    class _Key:
        key = rsa_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(auth_mod, "_jwks_client", lambda tenant: _Client())


def _entra_token(key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUD,
        "iat": now,
        "exp": now + 600,
        "oid": "oid-1",
        "name": "Rep One",
        "preferred_username": "rep1@contoso.com",
        "roles": ["FieldRep"],
        "territory": "TX-HOU-03",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def test_valid_entra_token(entra_settings, jwks, rsa_key):
    p = principal_from_token(_entra_token(rsa_key), entra_settings)
    assert p.roles == frozenset({Role.FIELD_REP}) and p.territory == "TX-HOU-03" and p.raw_token


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"aud": "api://someone-else"}, AuthenticationError),
        ({"iss": "https://evil.example/v2.0"}, AuthenticationError),
        ({"exp": int(time.time()) - 10}, AuthenticationError),
        ({"roles": []}, AuthorizationError),
        ({"roles": ["GlobalAdmin"]}, AuthorizationError),  # unknown roles never elevate
    ],
)
def test_invalid_entra_tokens(entra_settings, jwks, rsa_key, overrides, error):
    with pytest.raises(error):
        principal_from_token(_entra_token(rsa_key, **overrides), entra_settings)


def test_token_signed_by_other_key_rejected(entra_settings, jwks):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationError):
        principal_from_token(_entra_token(other), entra_settings)


PRINCIPAL = Principal(oid="oid-1", name="n", email="e", roles=frozenset({Role.FIELD_REP}), territory="T", raw_token="user-assertion")


async def test_obo_exchange_posts_correct_grant_and_caches(entra_settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        broker = OboBroker(entra_settings, http)
        t1 = await broker.acquire(PRINCIPAL, GRAPH_SCOPE)
        t2 = await broker.acquire(PRINCIPAL, GRAPH_SCOPE)
    assert t1 is t2 and len(seen) == 1
    form = seen[0]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["requested_token_use"] == "on_behalf_of"
    assert form["assertion"] == "user-assertion" and form["scope"] == GRAPH_SCOPE
    assert "graph-token" not in t1.fingerprint


async def test_obo_consent_error_is_surfaced(entra_settings):
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant", "suberror": "consent_required"}))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(DependencyUnavailableError) as exc:
            await OboBroker(entra_settings, http).acquire(PRINCIPAL, GRAPH_SCOPE)
    assert exc.value.details["error"] == "invalid_grant"


async def test_obo_requires_configuration(tmp_path):
    s = Settings(auth_mode=AuthMode.ENTRA, entra_tenant_id=TENANT, audit_db_path=tmp_path / "a.db")
    with pytest.raises(DependencyUnavailableError):
        await OboBroker(s).acquire(PRINCIPAL, GRAPH_SCOPE)


@pytest.mark.parametrize(("status", "raises"), [(200, False), (429, True), (503, True), (400, True)])
async def test_azure_openai_adapter_status_handling(tmp_path, status, raises):
    s = Settings(
        azure_openai_endpoint="https://aoai.example", azure_openai_api_key="k", llm_gateway_only=False, audit_db_path=tmp_path / "a.db"
    )
    provider = AzureOpenAIProvider(s)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-key"] == "k"
        assert "/openai/deployments/gpt-4o/chat/completions" in request.url.path
        return httpx.Response(status, json={"choices": [{"message": {"content": "hello"}}]})

    provider._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        if raises:
            with pytest.raises(DependencyUnavailableError):
                await provider.complete("sys", "user", max_tokens=50)
        else:
            assert await provider.complete("sys", "user", max_tokens=50) == "hello"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} hope that helps'])
def test_parse_json_tolerant(text):
    assert parse_json(text) == {"a": 1}

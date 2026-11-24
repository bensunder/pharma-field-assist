"""APIM AI-gateway path for the LLM: gateway-only enforcement, identity auth, token trace, throttling."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import DependencyUnavailableError
from app.llm.provider import AzureOpenAIProvider, LLMText, describe_llm_result, is_gateway_endpoint
from app.main import create_app
from app.services import ConfigurationError, Services
from tests.conftest import auth

GATEWAY = "https://apim-pfa-7dee6f.azure-api.net/aoai"
DIRECT = "https://aoai-pfa-7dee6f.openai.azure.com"


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self) -> str:
        self.calls += 1
        return "uami-token"


def gw_settings(tmp_path, **kw) -> Settings:
    base = dict(
        audit_db_path=tmp_path / "a.db",
        azure_openai_endpoint=GATEWAY,
        azure_openai_deployment="gpt-4o-mini",
        llm_gateway_only=True,
        log_level="WARNING",
    )
    base.update(kw)
    return Settings(**base)


def completion(content: str, prompt: int = 120, completion_tokens: int = 18) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion_tokens, "total_tokens": prompt + completion_tokens},
        },
    )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (GATEWAY, True),
        ("https://APIM-X.Azure-Api.NET/aoai", True),
        (DIRECT, False),
        ("https://evil.azure-api.net.example.com", False),
        (None, False),
    ],
)
def test_gateway_host_detection(endpoint, expected):
    assert is_gateway_endpoint(endpoint) is expected


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"azure_openai_endpoint": DIRECT}, "must be the APIM gateway"),
        ({"azure_openai_endpoint": None, "llm_required": True}, "required when PFA_LLM_REQUIRED"),
        ({"azure_openai_api_key": "k"}, "must be unset"),
    ],
)
def test_gateway_only_refuses_to_start_when_bypassable(tmp_path, overrides, message):
    with pytest.raises(ConfigurationError, match=message):
        Services.build(gw_settings(tmp_path, **overrides))


async def test_provider_calls_gateway_with_identity_token_and_reports_usage(tmp_path):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return completion("Start at 10 mg [1].")

    tokens = FakeTokens()
    provider = AzureOpenAIProvider(
        gw_settings(tmp_path), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), token_source=tokens
    )
    out = await provider.complete("sys", "user", max_tokens=100)
    req = seen[0]
    assert req.url.host == "apim-pfa-7dee6f.azure-api.net"
    assert req.url.path == "/aoai/openai/deployments/gpt-4o-mini/chat/completions"
    assert req.headers["Authorization"] == "Bearer uami-token" and "api-key" not in req.headers
    sent = json.loads(req.content)
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}  # JSON mode on every call
    assert isinstance(out, LLMText) and out == "Start at 10 mg [1]."
    assert out.via == "APIM gateway" and out.usage["prompt_tokens"] == 120
    assert describe_llm_result(out) == "via APIM gateway · 120 prompt / 18 completion tokens"
    assert provider.name == "azure-openai via APIM gateway"
    await provider.aclose()


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (429, "throttled"),
        (401, "rejected this app's identity"),
        (403, "rejected this app's identity"),
        (503, "returned 503"),
        (400, "rejected the request"),
    ],
)
async def test_provider_gateway_error_mapping(tmp_path, status, fragment):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(status, json={"statusCode": status, "message": "x"}, headers={"Retry-After": "12"})
    )
    provider = AzureOpenAIProvider(gw_settings(tmp_path), http=httpx.AsyncClient(transport=transport), token_source=FakeTokens())
    with pytest.raises(DependencyUnavailableError, match=fragment) as exc:
        await provider.complete("s", "u", max_tokens=100)
    assert "APIM gateway" in exc.value.message
    if status == 429:
        assert exc.value.details["retry_after"] == "12"
    await provider.aclose()


def test_describe_plain_strings_is_empty():
    assert describe_llm_result("plain") == ""


def _client(tmp_path, handler) -> TestClient:
    s = gw_settings(tmp_path)
    provider = AzureOpenAIProvider(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), token_source=FakeTokens())
    return TestClient(create_app(s, Services.build(s, llm=provider)))


def test_end_to_end_answer_goes_through_gateway_and_trace_shows_tokens(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] <= 400
        if "Sources:" not in body["messages"][1]["content"]:  # router call
            return completion(json.dumps({"intent": "product_qa", "confidence": 0.93, "rationale": "dosing"}), 90, 12)
        return completion(json.dumps({"grounded": True, "answer": "The recommended starting dose is 10 mg once daily [1]."}), 640, 21)

    with _client(tmp_path, handler) as c:
        t = c.post("/api/chat", json={"message": "What is the starting dose of Cardivex?"}, headers=auth(c, "rep")).json()
        assert t["route"]["method"] == "llm" and t["agent"] == "product_qa"
        llm_call = next(tc for tc in t["tool_calls"] if tc["tool"] == "llm.complete")
        assert "APIM gateway" in llm_call["identity"]
        assert llm_call["detail"] == "via APIM gateway · 640 prompt / 21 completion tokens"
        assert "10 mg" in t["answer"] and "Important Safety Information" in t["answer"]
        ops = c.get("/api/ops/status", headers=auth(c, "rep")).json()
        assert ops["llm"].startswith("azure-openai via APIM gateway")


def test_gateway_throttling_degrades_to_grounded_extractive_answer(tmp_path):
    with _client(tmp_path, lambda r: httpx.Response(429, json={"message": "Token limit exceeded"})) as c:
        t = c.post("/api/chat", json={"message": "What is the starting dose of Cardivex?"}, headers=auth(c, "rep")).json()
        assert t["route"]["method"] == "llm_fallback_rules"
        assert "10 mg once daily" in t["answer"] and t["citations"]
        llm_call = next(tc for tc in t["tool_calls"] if tc["tool"] == "llm.complete")
        assert llm_call["status"] == "error" and "throttled" in llm_call["detail"]


def test_gateway_only_is_the_default_and_blocks_direct_endpoints(tmp_path):
    s = Settings(audit_db_path=tmp_path / "a.db", azure_openai_endpoint=DIRECT, log_level="WARNING")
    assert s.llm_gateway_only is True
    with pytest.raises(ConfigurationError, match="must be the APIM gateway"):
        Services.build(s)


def test_gateway_only_default_blocks_api_keys_even_without_endpoint(tmp_path):
    s = Settings(audit_db_path=tmp_path / "a.db", azure_openai_api_key="k", log_level="WARNING")
    with pytest.raises(ConfigurationError, match="must be unset"):
        Services.build(s)


def test_no_llm_configured_starts_with_zero_model_spend(tmp_path):
    services = Services.build(Settings(audit_db_path=tmp_path / "a.db", log_level="WARNING"))
    assert services.settings.llm_enabled is False


def test_direct_endpoint_needs_explicit_opt_out(tmp_path):
    s = Settings(audit_db_path=tmp_path / "a.db", azure_openai_endpoint=DIRECT, llm_gateway_only=False, log_level="WARNING")
    Services.build(s)  # allowed only with the explicit opt-out


async def test_quota_exhausted_403_degrades_with_spend_cap_message(tmp_path):
    provider = AzureOpenAIProvider(
        gw_settings(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="Token quota exceeded"))),
        token_source=FakeTokens(),
    )
    with pytest.raises(DependencyUnavailableError, match="quota exhausted"):
        await provider.complete("sys", "user", max_tokens=100)


@pytest.mark.parametrize(("requested", "cap", "sent"), [(300, 400, 300), (5000, 400, 400), (300, 120, 120)])
async def test_max_tokens_is_clamped_to_the_configured_ceiling(tmp_path, requested, cap, sent):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return completion("{}")

    provider = AzureOpenAIProvider(
        gw_settings(tmp_path, llm_max_output_tokens=cap),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_source=FakeTokens(),
    )
    await provider.complete("s", "u", max_tokens=requested)
    assert seen[0]["max_tokens"] == sent
    await provider.aclose()


async def test_truncated_output_at_token_cap_is_rejected(tmp_path):
    reply = httpx.Response(200, json={"choices": [{"message": {"content": '{"grounded": true, "ans'}, "finish_reason": "length"}]})
    provider = AzureOpenAIProvider(
        gw_settings(tmp_path), http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: reply)), token_source=FakeTokens()
    )
    with pytest.raises(DependencyUnavailableError, match="truncated"):
        await provider.complete("s", "u", max_tokens=100)
    await provider.aclose()

"""LLM provider abstraction.

``AzureOpenAIProvider`` speaks the Azure OpenAI chat-completions contract. In Azure the endpoint is
the **APIM AI gateway** (``https://<apim>.azure-api.net/aoai``), never the Azure OpenAI account:

  container app --(UAMI token, aud=cognitiveservices)--> APIM
      validate-azure-ad-token (only this app's identity) -> azure-openai-token-limit
      -> azure-openai-emit-token-metric -> authentication-managed-identity (APIM's own MI)
  --> Azure OpenAI (local auth disabled; only APIM's identity holds "OpenAI User")

With ``PFA_LLM_GATEWAY_ONLY=true`` the app refuses to start unless the endpoint is an APIM gateway
host, so a configuration slip can't route model traffic around the gateway. API keys are only for
local development against a direct endpoint.

``NullProvider`` is the offline mode: deterministic routing + extractive answers, which is also the
graceful-degradation path when the gateway throttles (429) or the model is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Generic, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.core.errors import DependencyUnavailableError

log = logging.getLogger(__name__)

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
GATEWAY_HOST_SUFFIXES = (".azure-api.net",)


def is_gateway_endpoint(endpoint: str | None) -> bool:
    host = (urlparse(endpoint or "").hostname or "").lower()
    return any(host.endswith(sfx) for sfx in GATEWAY_HOST_SUFFIXES)


class LLMText(str):
    """A ``str`` that also carries call metadata (token usage, route), so existing callers that
    treat the result as text keep working while the trace can show what the gateway reported."""

    usage: dict
    via: str

    def __new__(cls, text: str, *, usage: dict | None = None, via: str = "direct") -> LLMText:
        obj = super().__new__(cls, text)
        obj.usage = usage or {}
        obj.via = via
        return obj


def describe_llm_result(result: object) -> str:
    """Trace detail for an LLM tool call: route + token counts when available."""
    via = getattr(result, "via", None)
    usage = getattr(result, "usage", None) or {}
    if not via:
        return ""
    parts = [f"via {via}"]
    if usage:
        parts.append(f"{usage.get('prompt_tokens', '?')} prompt / {usage.get('completion_tokens', '?')} completion tokens")
    return " · ".join(parts)


class LLMProvider(Protocol):
    name: str
    enabled: bool

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...

    async def aclose(self) -> None: ...


class NullProvider:
    name = "none"
    enabled = False

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        raise DependencyUnavailableError("No LLM configured")

    async def aclose(self) -> None:
        return None


class _ManagedIdentityToken:
    """Token source for the container's user-assigned identity.

    Uses the *sync* azure-identity credential on a worker thread (the async credential needs aiohttp,
    which the image doesn't ship). Tokens are cached until five minutes before expiry.
    """

    def __init__(self, client_id: str | None) -> None:
        from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]

        self._cred = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id else DefaultAzureCredential()
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and self._expires - 300 > time.time():
                return self._token
            tok = await asyncio.to_thread(self._cred.get_token, COGNITIVE_SCOPE)
            self._token, self._expires = tok.token, float(tok.expires_on)
            return self._token

    def close(self) -> None:
        self._cred.close()


class AzureOpenAIProvider:
    enabled = True

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None, token_source=None) -> None:
        self._s = settings
        self._http = http or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self.via_gateway = is_gateway_endpoint(settings.azure_openai_endpoint)
        self.name = "azure-openai via APIM gateway" if self.via_gateway else "azure-openai (direct)"
        self._tokens = token_source
        if self._tokens is None and not settings.azure_openai_api_key:
            try:
                import os

                self._tokens = _ManagedIdentityToken(os.environ.get("AZURE_CLIENT_ID"))
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("Set PFA_AZURE_OPENAI_API_KEY or install azure-identity") from exc

    async def _headers(self) -> dict[str, str]:
        if self._tokens is not None:
            return {"Authorization": f"Bearer {await self._tokens.get()}"}
        return {"api-key": self._s.azure_openai_api_key or ""}

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        """Raw call: ALWAYS JSON mode, max_tokens clamped to the configured ceiling. Agents must not
        call this directly - use ``complete_json`` so the output is also schema-validated."""
        url = (
            f"{self._s.azure_openai_endpoint.rstrip('/')}/openai/deployments/"  # type: ignore[union-attr]
            f"{self._s.azure_openai_deployment}/chat/completions?api-version={self._s.azure_openai_api_version}"
        )
        body: dict = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": clamp_max_tokens(max_tokens, self._s.llm_max_output_tokens),
            "response_format": {"type": "json_object"},
        }
        target = "APIM gateway" if self.via_gateway else "Azure OpenAI"
        try:
            resp = await self._http.post(url, headers=await self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"{target} unreachable") from exc
        if resp.status_code == 429:
            # APIM azure-openai-token-limit (or AOAI quota): degrade, don't retry-storm
            raise DependencyUnavailableError(f"{target} throttled (429)", details={"retry_after": resp.headers.get("Retry-After")})
        if resp.status_code >= 500:
            raise DependencyUnavailableError(f"{target} returned {resp.status_code}")
        if resp.status_code == 403 and self.via_gateway and any(w in resp.text.lower() for w in ("quota", "limit")):
            # APIM azure-openai-token-limit token-quota exhausted (daily/monthly spend cap): degrade
            log.warning("llm_quota_exhausted", extra={"status": 403, "body": resp.text[:300]})
            raise DependencyUnavailableError(f"{target} token quota exhausted (403) - spend cap reached")
        if resp.status_code in (401, 403):
            log.warning("llm_auth_rejected", extra={"status": resp.status_code, "body": resp.text[:300]})
            raise DependencyUnavailableError(f"{target} rejected this app's identity ({resp.status_code})")
        if resp.status_code >= 400:
            # content-filter hits come back as 400 with code content_filter
            log.warning("llm_rejected", extra={"status": resp.status_code, "body": resp.text[:300]})
            raise DependencyUnavailableError(f"{target} rejected the request")
        data = resp.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            # Hit the token cap mid-answer: the JSON is truncated, never pass partial output on
            raise DependencyUnavailableError(f"{target} output truncated at the {body['max_tokens']}-token cap")
        return LLMText(
            choice["message"]["content"] or "",
            usage=data.get("usage") or {},
            via="APIM gateway" if self.via_gateway else "direct",
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        if isinstance(self._tokens, _ManagedIdentityToken):
            self._tokens.close()


def clamp_max_tokens(requested: int, ceiling: int) -> int:
    return max(1, min(int(requested), int(ceiling)))


T = TypeVar("T", bound=BaseModel)


class StructuredResult(Generic[T]):
    """Schema-validated model output plus the call metadata the trace shows (route, token usage)."""

    def __init__(self, data: T, raw: object) -> None:
        self.data = data
        self.usage = getattr(raw, "usage", None) or {}
        self.via = getattr(raw, "via", None)


async def complete_json(llm: LLMProvider, system: str, user: str, *, schema: type[T], max_tokens: int) -> StructuredResult[T]:
    """The ONLY way agents talk to the model: JSON object out, capped tokens, validated by ``schema``.

    Anything that is not a JSON object matching the schema raises DependencyUnavailableError, so
    callers take their deterministic fallback instead of passing free text to users.
    """
    raw = await llm.complete(system, user, max_tokens=max_tokens)
    try:
        return StructuredResult(schema.model_validate(parse_json(str(raw))), raw)
    except (ValueError, ValidationError) as exc:
        log.warning("llm_output_rejected", extra={"schema": schema.__name__, "reason": str(exc)[:200]})
        raise DependencyUnavailableError(f"model output failed {schema.__name__} validation") from exc


def build_provider(settings: Settings) -> LLMProvider:
    return AzureOpenAIProvider(settings) if settings.llm_enabled else NullProvider()


def parse_json(text: str) -> dict:
    """Tolerant JSON extraction (models sometimes wrap JSON in code fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])

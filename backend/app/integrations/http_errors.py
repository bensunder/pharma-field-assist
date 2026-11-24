"""Shared HTTP -> domain error mapping for live integrations.

Retryable (counts toward the circuit breaker): timeouts, connection errors, 429, 5xx.
Not retryable (surfaced immediately with a precise message, breaker untouched): 400/401/403/404 —
those are configuration or permission problems, and retrying them only burns quota.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from app.core.errors import DependencyUnavailableError
from app.resilience.breaker import TransientError


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):  # Graph: {"error": {"code", "message"}}
            return f"{err.get('code')}: {err.get('message', '')[:160]}"
        if isinstance(err, str):  # OAuth: {"error", "error_description"}
            return f"{err}: {str(body.get('error_description', ''))[:160]}"
    if isinstance(body, list) and body and isinstance(body[0], dict):  # Salesforce: [{"errorCode","message"}]
        return f"{body[0].get('errorCode')}: {str(body[0].get('message', ''))[:160]}"
    return str(body)[:200]


def check(resp: httpx.Response, dependency: str) -> httpx.Response:
    if resp.status_code < 400:
        return resp
    detail = _error_text(resp)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientError(f"{dependency} HTTP {resp.status_code} {detail}")
    hint = {
        401: "authentication failed — token rejected",
        403: "permission denied — check API permissions / admin consent / sharing",
        404: "not found — check mailbox, object or endpoint",
    }.get(resp.status_code, "request rejected")
    raise DependencyUnavailableError(
        f"{dependency}: {hint}", details={"dependency": dependency, "status": resp.status_code, "error": detail}
    )


async def send(dependency: str, call: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
    try:
        resp = await call()
    except httpx.TimeoutException as exc:
        raise TransientError(f"{dependency} timed out") from exc
    except httpx.HTTPError as exc:
        raise TransientError(f"{dependency} unreachable: {type(exc).__name__}") from exc
    return check(resp, dependency)

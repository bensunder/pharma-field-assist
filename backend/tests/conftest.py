from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import DependencyUnavailableError
from app.main import create_app
from app.services import Services


class FakeLLM:
    """Scriptable LLM double: pass a function (system, user) -> str, or raise to simulate outage."""

    name = "fake"
    enabled = True

    def __init__(self, fn: Callable[[str, str], str] | None = None, *, down: bool = False) -> None:
        self.fn = fn or (lambda s, u: "")
        self.down = down
        self.calls: list[tuple[str, str]] = []
        self.max_tokens: list[int] = []

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        self.calls.append((system, user))
        self.max_tokens.append(max_tokens)
        if self.down:
            raise DependencyUnavailableError("fake llm down")
        return self.fn(system, user)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(audit_db_path=tmp_path / "audit.db", log_level="WARNING", breaker_reset_seconds=30)


@pytest.fixture
def make_client(settings) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(llm=None, **overrides) -> TestClient:
        s = settings.model_copy(update=overrides)
        app = create_app(s, Services.build(s, llm=llm))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def auth(client: TestClient, persona: str) -> dict[str, str]:
    token = client.post("/api/auth/demo-token", json={"persona": persona}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def chat(client: TestClient, persona: str, message: str) -> dict:
    r = client.post("/api/chat", json={"message": message}, headers=auth(client, persona))
    assert r.status_code == 200, r.text
    return r.json()


def outcomes(turn: dict) -> dict[str, str]:
    return {v["rule_id"]: v["outcome"] for v in turn["verdicts"]}

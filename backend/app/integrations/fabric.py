"""Microsoft Fabric analytics integration.

* ``FabricSqlClient`` — LIVE. Queries the Lakehouse **SQL analytics endpoint** (TDS) with
  ``mssql-python`` (Microsoft's driver; pip-only, bundles its own ODBC layer — no system install).
  Auth: an Entra app-only token for ``https://database.windows.net/.default``; the service principal
  needs Viewer (or higher) on the Fabric workspace.
* ``MockFabricClient`` — local JSON for the offline demo.

Only parameterised, whitelisted aggregates are supported: the metric must be in ``METRICS``, the
table name is validated at start-up, and every value is a bind parameter. The LLM never writes SQL.
Territory scoping is enforced in the WHERE clause (defence in depth alongside Fabric RLS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from app.config import Settings
from app.core.errors import DependencyUnavailableError
from app.resilience.breaker import CircuitBreaker, TransientError
from app.resilience.faults import FaultRegistry
from app.security.app_tokens import SQL_SCOPE, ClientCredentialsProvider

log = logging.getLogger(__name__)

METRICS = ("trx", "nrx", "market_share")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class AnalyticsClient(Protocol):
    dependency: str
    breaker: CircuitBreaker
    mode: str
    dataset_name: str
    refreshed_at: str

    def identity(self) -> str: ...
    async def weekly_series(self, *, metric: str, product: str, territory_prefix: str, weeks: int) -> dict[str, list[dict]]: ...


def _like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[") + "%"


def build_weekly_query(table: str, metric: str) -> str:
    if metric not in METRICS:
        raise ValueError(f"unsupported metric {metric}")
    if not _IDENT.match(table):
        raise ValueError(f"invalid table name {table!r}")
    # identifiers are whitelisted/validated above; every value is a bind parameter
    return (
        f"WITH w AS (SELECT DISTINCT TOP (?) week_start FROM dbo.[{table}] WHERE product = ? ORDER BY week_start DESC) "  # noqa: S608
        f"SELECT CONVERT(varchar(10), r.week_start, 23) AS week_start, r.territory, CAST(r.[{metric}] AS float) AS value "
        f"FROM dbo.[{table}] AS r JOIN w ON r.week_start = w.week_start "
        "WHERE r.product = ? AND (? = 'US' OR r.territory LIKE ? ESCAPE '\\') "
        "ORDER BY r.territory, r.week_start"
    )


def _rows_to_series(rows: list[tuple]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for week_start, territory, value in rows:
        out[territory].append({"week_start": str(week_start), "value": float(value)})
    return dict(out)


# ============================================================================== live
def _default_connect(conn_str: str, token: str, timeout: int) -> Any:  # pragma: no cover - needs a live endpoint
    import mssql_python  # imported lazily: only live mode needs the native driver

    provider = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token=token))
    return mssql_python.connect(conn_str, token_provider=provider, timeout=timeout)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return name in {"OperationalError", "InterfaceError"} or any(
        s in text for s in ("timeout", "timed out", "connection", "throttl", "40613", "40501", "10928", "10929")
    )


class FabricSqlClient:
    dependency = "fabric"
    mode = "live"

    def __init__(
        self,
        settings: Settings,
        breaker: CircuitBreaker,
        faults: FaultRegistry,
        app_tokens: ClientCredentialsProvider,
        connect: Callable[[str, str, int], Any] = _default_connect,
    ) -> None:
        if not _IDENT.match(settings.fabric_table):
            raise ValueError(f"PFA_FABRIC_TABLE {settings.fabric_table!r} is not a valid identifier")
        self.breaker = breaker
        self._faults = faults
        self._tokens = app_tokens
        self._connect = connect
        self._table = settings.fabric_table
        self._timeout = int(settings.integration_timeout_seconds)
        self._conn_str = (
            f"Server=tcp:{settings.fabric_sql_endpoint},1433;Database={settings.fabric_database};Encrypt=yes;TrustServerCertificate=no;"
        )
        self.dataset_name = f"{settings.fabric_database}.dbo.{settings.fabric_table} (Fabric SQL endpoint)"
        self.refreshed_at = "live query"

    def identity(self) -> str:
        return "app:service-principal (Fabric SQL endpoint)"

    def _run(self, token: str, sql: str, params: tuple) -> list[tuple]:
        conn = self._connect(self._conn_str, token, self._timeout)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return [tuple(r) for r in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()

    async def weekly_series(self, *, metric: str, product: str, territory_prefix: str, weeks: int) -> dict[str, list[dict]]:
        sql = build_weekly_query(self._table, metric)
        params = (int(weeks), product, product, territory_prefix, _like_prefix(territory_prefix))

        async def _op() -> dict[str, list[dict]]:
            self._faults.maybe_raise(self.dependency)
            token = (await self._tokens.get(SQL_SCOPE)).access_token
            try:
                rows = await asyncio.to_thread(self._run, token, sql, params)
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientError(f"fabric: {type(exc).__name__}") from exc
                log.warning("fabric_query_failed", extra={"error": str(exc)[:300]})
                raise DependencyUnavailableError(
                    "fabric: query rejected — check workspace access, database and table names",
                    details={"dependency": "fabric", "error": type(exc).__name__},
                ) from exc
            return _rows_to_series(rows)

        return await self.breaker.call(_op)


# ============================================================================== mock
class MockFabricClient:
    dependency = "fabric"
    mode = "mock"

    def __init__(self, dataset: Path, breaker: CircuitBreaker, faults: FaultRegistry) -> None:
        self.breaker = breaker
        self._faults = faults
        payload = json.loads(dataset.read_text(encoding="utf-8"))
        self.dataset_name: str = payload["dataset"] + " (mock)"
        self.refreshed_at: str = payload["refreshed_at"]
        self._rows: list[dict] = payload["rows"]

    def identity(self) -> str:
        return "app:managed-identity (mock Fabric)"

    async def weekly_series(self, *, metric: str, product: str, territory_prefix: str, weeks: int) -> dict[str, list[dict]]:
        if metric not in METRICS:
            raise ValueError(f"unsupported metric {metric}")

        async def _op() -> dict[str, list[dict]]:
            self._faults.maybe_raise(self.dependency)
            window = set(sorted({r["week_start"] for r in self._rows if r["product"] == product})[-weeks:])
            rows = [
                (r["week_start"], r["territory"], r[metric])
                for r in sorted(self._rows, key=lambda r: (r["territory"], r["week_start"]))
                if r["product"] == product
                and r["week_start"] in window
                and (territory_prefix == "US" or r["territory"].startswith(territory_prefix))
            ]
            return _rows_to_series(rows)

        return await self.breaker.call(_op)

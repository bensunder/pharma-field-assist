"""Pre-flight check for live integrations — run this before the demo.

Builds the exact same Services the API uses and makes one real read call per live dependency,
printing PASS/FAIL with the precise upstream error (missing consent, wrong mailbox, no workspace
access...). Exit code 0 only if every live integration passed.

    cd backend && python -m scripts.check_live
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta

from app.core.errors import AppError
from app.integrations.graph_calendar import CENTRAL
from app.security.auth import DEMO_PERSONAS
from app.security.obo import DelegatedToken
from app.services import ConfigurationError, Services


async def main() -> int:
    from app.config import Settings

    settings = Settings()
    try:
        services = Services.build(settings)
    except ConfigurationError as exc:
        print(f"CONFIG  FAIL  {exc}")
        return 1

    rep = DEMO_PERSONAS["rep"]
    who = DelegatedToken(access_token="", scope="", subject_oid=rep["oid"], expires_at=time.time() + 600, subject_upn=rep["email"])
    modes = services.integration_modes()
    print("modes:", modes)
    failures = 0

    async def probe(name: str, check) -> None:
        nonlocal failures
        if modes[name] != "live":
            print(f"{name:7} SKIP  (mock mode)")
            return
        started = time.perf_counter()
        try:
            detail = await check()
            print(f"{name:7} PASS  {detail}  ({(time.perf_counter() - started) * 1000:.0f} ms)")
        except AppError as exc:
            failures += 1
            print(f"{name:7} FAIL  {exc.message}  {exc.details or ''}")
        except Exception as exc:
            failures += 1
            print(f"{name:7} FAIL  {type(exc).__name__}: {exc}")

    day = datetime.now(CENTRAL).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)

    async def graph() -> str:
        busy = await services.graph.get_busy(who, day)
        return f"{len(busy)} busy block(s) on {day} via {services.graph.identity(who)}"

    async def crm() -> str:
        accounts = await services.crm.list_accounts(who, rep["territory"])
        names = ", ".join(a["name"] for a in accounts[:4]) or "none — run python -m scripts.seed_salesforce"
        return f"{len(accounts)} account(s) in {rep['territory']}: {names}"

    async def fabric() -> str:
        series = await services.fabric.weekly_series(metric="trx", product="cardivex", territory_prefix=rep["territory"], weeks=4)
        points = sum(len(v) for v in series.values())
        return f"{points} weekly point(s) for {rep['territory']} from {services.fabric.dataset_name}"

    await probe("graph", graph)
    await probe("crm", crm)
    await probe("fabric", fabric)
    await services.aclose()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

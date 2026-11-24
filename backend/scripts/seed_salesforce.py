"""Seed the demo HCP accounts into a Salesforce org (idempotent upsert on AccountNumber).

Uses the same settings as the API (PFA_SF_* from the environment or backend/.env), and the same
``AccountMapper`` so the field mapping can't drift from what the app reads.

    cd backend && python -m scripts.seed_salesforce            # upsert 7 demo accounts
    cd backend && python -m scripts.seed_salesforce --dry-run  # print records only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

from app.config import BACKEND_ROOT, SalesforceAuth, Settings
from app.integrations.crm import AccountMapper, SalesforceCrmClient, soql_literal
from app.resilience.breaker import CircuitBreaker
from app.resilience.faults import FaultRegistry
from app.security.obo import DelegatedToken


async def main(dry_run: bool) -> int:
    settings = Settings()
    accounts = json.loads((BACKEND_ROOT / "data" / "crm_accounts.json").read_text())["accounts"]
    records = [AccountMapper.to_record(a) for a in accounts]
    if dry_run:
        print(json.dumps(records, indent=2))
        return 0
    if settings.sf_auth != SalesforceAuth.CLIENT_CREDENTIALS:
        print("Seeding uses the integration user: set PFA_SF_AUTH=client_credentials for this script.", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=20) as http:
        client = SalesforceCrmClient(settings, CircuitBreaker("seed"), FaultRegistry(), http)
        who = DelegatedToken(access_token="", scope="", subject_oid="seed", expires_at=time.time() + 600, subject_upn="seed")
        for rec in records:
            q = f"SELECT Id FROM Account WHERE AccountNumber = '{soql_literal(rec['AccountNumber'])}' LIMIT 1"  # noqa: S608
            existing = await client._query(who, q)
            if existing:
                await client._request(who, "PATCH", f"/sobjects/Account/{existing[0]['Id']}", json=rec)
                print(f"updated  {rec['AccountNumber']}  {rec['Name']}  ({rec['Site']})")
            else:
                resp = await client._request(who, "POST", "/sobjects/Account", json=rec)
                print(f"created  {rec['AccountNumber']}  {rec['Name']}  ({rec['Site']})  -> {resp.json()['id']}")
    print("Done. Demo persona Jordan Reyes (TX-HOU-03) will see 4 of these; the district manager sees 6.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args().dry_run)))

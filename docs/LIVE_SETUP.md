# Going live: Microsoft Graph, Salesforce and Fabric

Each integration switches on its own (`PFA_GRAPH_MODE`, `PFA_CRM_MODE`, `PFA_FABRIC_MODE` = `live|mock`). A live mode with missing settings **refuses to start** and names the missing variable. Plan on about 45 minutes the first time.

| Integration | What you need | Auth used by the app |
|---|---|---|
| Graph (Outlook) | An Entra tenant where you're Global Admin, plus one Exchange Online mailbox | Demo personas: app-only client credentials → `/users/{mailbox}`. Entra mode: delegated OBO → `/me` |
| Salesforce (stand-in for Veeva) | A free Developer Edition org | Client credentials (integration user), or JWT bearer (per-rep) |
| Fabric | A Fabric trial or capacity workspace with a Lakehouse | Service principal → SQL analytics endpoint (TDS) |

---

## 1. Salesforce (≈10 min, manual UI)
1. Sign up for a Developer Edition org at developer.salesforce.com/signup.
2. **Setup → My Domain**: copy the URL, e.g. `https://yourco-dev-ed.develop.my.salesforce.com`. Client credentials only works against My Domain.
3. **Setup → App Manager → New External Client App** (or *New Connected App* in older orgs):
   - Enable OAuth, callback `http://localhost`, scope **Manage user data via APIs (api)**
   - Enable **Client Credentials Flow**
   - Policies → **Run As**: your admin user (this is the integration user)
4. Copy the **Consumer Key** and **Consumer Secret**.

> **Real Veeva:** classic Veeva CRM runs on Salesforce, so the same REST calls work. Change the field names in `AccountMapper` (`backend/app/integrations/crm.py`), e.g. `Specialty_1_vod__c`, `Call2_vod__c`, `Territory_vod__c`. For per-rep sharing rules use `PFA_SF_AUTH=jwt_bearer` with a certificate on the connected app and `PFA_SF_JWT_PRIVATE_KEY_PATH`.

## 2. Fabric (≈10 min)
1. Go to app.fabric.microsoft.com and start a trial if needed. Create a workspace (e.g. `PharmaFieldAssist`).
2. **New → Lakehouse** named `pfa_lakehouse`.
3. **Files → Upload** `backend/data/fabric_weekly_rx_by_territory.csv` → right-click it → **Load to Tables → New table** named `weekly_rx_by_territory`.
4. Copy the workspace ID from the URL (`/groups/<workspace-id>/`).
5. Admin portal → Tenant settings → **Service principals can use Fabric APIs**: enable it (scoped to a security group is fine) if your tenant has it off.

## 3. Entra + Graph + wiring: one script (≈2 min)
```bash
az login --tenant <your-tenant> --allow-no-subscriptions

MAILBOX=jordan@yourtenant.onmicrosoft.com \
FABRIC_WORKSPACE_ID=<workspace-guid> FABRIC_LAKEHOUSE=pfa_lakehouse \
SF_LOGIN_URL=https://yourco-dev-ed.develop.my.salesforce.com SF_CLIENT_ID=<key> SF_CLIENT_SECRET=<secret> \
scripts/setup-live-azure.sh
```
The script creates the app registration and service principal and grants **Calendars.ReadWrite** (application) with admin consent. It also creates a secret, gives the service principal Viewer on the Fabric workspace, resolves the Lakehouse SQL endpoint, and writes `backend/.env` (mode 600; any previous one is backed up).

## 4. Seed and check
```bash
cd backend
python -m scripts.seed_salesforce     # upserts 7 demo HCP accounts (idempotent)
python -m scripts.check_live          # one real read per live integration -> PASS / FAIL with the exact upstream error
```
Expected:
```
graph   PASS  3 busy block(s) on 2026-11-24 via app:client-credentials → mailbox jordan@…
crm     PASS  4 account(s) in TX-HOU-03: Dr. Anita Patel, Dr. Lena Kowalski, …
fabric  PASS  4 weekly point(s) for TX-HOU-03 from pfa_lakehouse.dbo.weekly_rx_by_territory (Fabric SQL endpoint)
```

## 5. Run
`docker compose up --build` (reads `backend/.env`) or `scripts/dev.sh`. **Platform ops** shows a `live` chip for each live dependency, and the **Agent trace** shows which identity made each call.

---

## Troubleshooting

| Symptom (`check_live` or trace) | Cause | Fix |
|---|---|---|
| `graph: permission denied … ErrorAccessDenied` | Consent not applied yet | Wait 2–5 min, re-run. Confirm in Entra → App registrations → API permissions |
| `graph: not found` | Wrong UPN or no Exchange license | Fix `PFA_GRAPH_DEMO_MAILBOX` |
| `Salesforce login failed … invalid_grant` | Client credentials not enabled, no Run As user, or wrong URL | Recheck step 1.3. Must use the My Domain URL |
| `crm` PASS with `0 account(s)` | Not seeded | `python -m scripts.seed_salesforce` |
| `fabric: query rejected` | Table name or schema, or SP lacks workspace access | Check the table is under Tables (not Files). Add the SP as Viewer |
| `fabric … OperationalError` | Endpoint unreachable, or tenant setting off | Check `PFA_FABRIC_SQL_ENDPOINT`. Enable the SP tenant setting (step 2.5) |

## Security notes
- An application permission on `Calendars.ReadWrite` reaches **every mailbox in the tenant**. Outside a dev tenant, scope it with Exchange **RBAC for Applications**. In production the app uses delegated OBO (`PFA_AUTH_MODE=entra`) and no application permission at all.
- `backend/.env` holds secrets. It's in `.gitignore` and `.dockerignore`, and compose injects it at runtime. In Azure these values come from Key Vault references (`infra/modules/keyvault.bicep`).
- Bookings use the HITL action ID as Graph's `transactionId`, so a retried confirm can never double-book.

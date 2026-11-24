# PharmaFieldAssist — Production Runbook

## Severity & SLA

| Sev | Definition | Examples | Ack | Mitigate | Comms |
|---|---|---|---|---|---|
| **1** | Platform down or a compliance control failing for many users | API 5xx > 5%, auth broken for all reps, **AE routing or PII redaction not working**, audit writes failing | 15 min | 1 h | Bridge + client lead every 30 min |
| **2** | A major capability degraded, workaround exists | Graph/Veeva/Fabric breaker open, LLM endpoint throttled (429) → extractive mode, replica crash-loop | 30 min | 4 h | Status update every hour |
| **3** | Minor or single-user impact | p95 > 8 s, one territory's data stale, a single KB answer wrong | 4 h (business) | 2 business days | Ticket |
| **4** | Cosmetic or request | UI glitch, new synonym, copy change | 1 business day | Next release | Ticket |

> A compliance control that fails open (AE not routed, PII stored, expired content served) is **always Sev 1**, even for a single user. Notify Compliance and Pharmacovigilance immediately.

## First 10 minutes (any Sev 1/2)
1. Acknowledge the alert, open an incident in ADO Boards, start the timeline.
2. **Was there a release in the last 2 hours?** `az containerapp revision list -g <rg> -n ca-pfa-prod-api -o table`
   If so and symptoms match → **roll back first, diagnose second**:
   `scripts/aca-rollback.sh <rg> ca-pfa-prod-api`. This shifts traffic to the `blue` revision, with no rebuild.
3. Check dependency state: `GET /api/ops/status` (breakers), and Azure Status for Graph / AOAI regions.
4. Pull a failing request by correlation ID (reps see it in error messages):

```kusto
ContainerAppConsoleLogs_CL
| where Log_s has "<correlation-id>"
| project TimeGenerated, RevisionName_s, Log_s
| order by TimeGenerated asc
```

## Playbooks

### API 5xx spike (Sev 1)
```kusto
requests | where timestamp > ago(30m) and toint(resultCode) >= 500
| summarize count() by name, resultCode, cloud_RoleInstance, bin(timestamp, 1m)
```
- Concentrated on one revision → rollback.
- `unhandled_error` in logs → find the exception by correlation ID, hotfix through a PR (expedited CAB).
- Readiness failing (`audit_store: false`) → check the storage/DB connection; the app stays unready on purpose rather than serving unaudited turns.

### Dependency breaker open (Sev 2): Graph / Veeva / Fabric
```kusto
ContainerAppConsoleLogs_CL | where Log_s has "breaker_opened" or Log_s has "obo_exchange_failed"
| extend p = parse_json(Log_s) | project TimeGenerated, dependency = tostring(p.dependency), p
```
- Users get a degraded message and **nothing is written** (HITL actions stay pending for retry).
- `obo_exchange_failed` with `invalid_grant` / `consent_required` → an Entra-side change (Conditional Access, consent revoked). Engage the identity team.
- After the upstream recovers, the breaker closes itself after 30 s (half-open probe). Force it with `POST /api/ops/breakers/{name}/reset`.

### LLM throttling / outage (Sev 2)
- The router falls back to rules (`route_method = llm_fallback_rules`) and product Q&A falls back to extractive answers from the same approved sources. Answers stay grounded but less fluent.
- Check AOAI metrics (429s, TPM). Raise the quota or fail over to the secondary deployment (`PFA_AZURE_OPENAI_DEPLOYMENT`), which ships as a new revision.

### Suspected compliance failure (Sev 1)
1. Reproduce with the exact sanitized input from the audit record (`chat.turn.payload.input_sanitized`).
2. Check `policy_version` and `kb_version` on the record. If they changed recently, revert the policy/KB PR.
3. Run `GET /api/audit/verify`. A broken chain means tampering → security incident process.
4. For a missed AE: export the affected turns and hand them to Pharmacovigilance for manual case intake **within 24 h of awareness**.

### Secret rotation (planned, Sev 4)
1. Write a new version of the secret in Key Vault (OBO client secret, App Insights connection string).
2. `az containerapp revision restart -g <rg> -n ca-pfa-<env>-api --revision <live>`. Key Vault references resolve the latest version on start.
3. Confirm `/health/ready` and a smoke test: `scripts/smoke.sh https://<fqdn>`.

## Release / rollback reference
| Action | Command |
|---|---|
| Release (pipeline does this) | `scripts/aca-release.sh <rg> <app> <acr>/pfa-api:<sha> <canary-seconds>` |
| Roll back to previous | `scripts/aca-rollback.sh <rg> <app>` |
| Roll back to a specific revision | `scripts/aca-rollback.sh <rg> <app> <revision-name>` |
| Traffic view | `az containerapp ingress traffic show -g <rg> -n <app> -o table` |

## Post-incident
Write a blameless RCA within 5 business days: timeline, root cause, detection gap, and an action item backed by a **regression test**. Every Sev 1/2 fix ships with a test that reproduces it.

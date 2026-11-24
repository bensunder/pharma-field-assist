# Demo script — PharmaFieldAssist (12–15 min)

**Positioning line (say this first):** "I built a small version of the platform you described: a FastAPI + React multi-agent assistant for pharma field reps. It has compliance controls, audit, Entra/OBO integrations and Azure Container Apps operations. It runs offline for this demo. The same code switches to Azure OpenAI and Entra ID through environment variables."

Products, HCPs and data are **fictional** (Cardivex/velostatin, Respira/tiolanide). Graph, Salesforce and Fabric are **live** when `backend/.env` is configured (docs/LIVE_SETUP.md). Run `python -m scripts.check_live` 30 minutes before the call.

Start: `docker compose up --build` → http://localhost:8080 (or `scripts/dev.sh` → http://localhost:5173)

---

## 1. Product Q&A: grounded, cited and fair-balanced (2 min) · *Field Rep*
Click **"What is the starting dose of Cardivex?"**

Point at the **Agent trace** panel:
- `input_guardrails → route → authz → agent → output_guardrails → audit`. Each step maps to a LangGraph node.
- Route method `rules` (offline). With Azure OpenAI it shows `llm`, and it falls back to rules if the model errors.
- **Citation** carries the MLR approval ID, `CMP-103 amended`, and the Important Safety Information is appended automatically (fair balance).
- Audit record number and hash. The policy version and KB version are stamped on the record.

Talking point: *"The knowledge pipeline is Markdown with front matter. Content with an expired or missing MLR approval is excluded when the index is built."* → Open **Platform ops**: *Cardivex Launch Messaging 2025* shows **excluded · MLR approval expired**.
Then switch to *Compliance* and ask **"Tell me about Cardivex launch messaging 2025"** → `CMP-101 blocked`. With no approved source there's no answer. It never makes one up.

## 1b. LLM through the APIM AI gateway (1 min)
In the trace, `route: llm` and the `llm.complete` tool call show **`app:managed-identity → APIM gateway → Azure OpenAI`** and the token counts APIM reported. Then the portal:
- **APIM → APIs → Azure OpenAI → Policies:** `validate-azure-ad-token` (only this app's identity), `azure-openai-token-limit`, `azure-openai-emit-token-metric`, `authentication-managed-identity`.
- **Azure OpenAI → Keys:** disabled. Only APIM's identity holds *Cognitive Services OpenAI User*.
- **App Insights → Metrics → namespace `pfa-llm`:** tokens by client.

Talking point: *"The app can't bypass the gateway. With `PFA_LLM_GATEWAY_ONLY=true` it refuses to start unless the endpoint is APIM, and there are no keys to leak. When APIM's token limit returns a 429, the app falls back to extractive answers from the same approved sources."*

## 2. Multi-agent workflows with human-in-the-loop (3 min) · *Field Rep*
1. **"When am I free Thursday?"** → scheduling agent; tool call `graph.getSchedule`, identity `user:<oid> (OBO)`, scope `Calendars.ReadWrite`, token fingerprint (the token itself is never logged).
2. **"Schedule a meeting with Dr. Patel tomorrow at 2pm"** → checks for conflicts, enriches the attendee from Veeva (`crm.findAccount`), then a **Human approval required** card → **Confirm** → Outlook event created, `action.completed` audited.
3. **"Show my Cardivex TRx for the last 8 weeks"** → analytics agent on Fabric; the model never writes SQL, it fills a whitelisted query spec.
4. **"log a call with Dr. Okafor: discussed Cardivex formulary coverage"** → CRM write with confirmation.

**Live proof:** after Confirm, open Outlook on the web for the demo mailbox. The event is there with category *PharmaFieldAssist*. After logging a call, open the account in Salesforce → Activity, where the completed Task is shown. The trace shows `app:client-credentials → mailbox …` and `app:salesforce-integration-user`. *"In production with Entra sign-in, the same code calls Graph `/me` with an OBO token, so it is the rep's own identity and the app holds no application permission."*

Talking point: *"Reads run straight through. Writes into Outlook or Veeva always need a confirmation that is bound to the user and expires. A confirmation that fails, say because Graph is down, stays pending so the rep can retry. It's never lost and never double-booked."*

## 3. Compliance controls (3 min) · *Field Rep*
- **"Dr. Patel said her patient John Smith had muscle pain after starting Cardivex"** → `CMP-001 redacted (PATIENT_NAME)` and `CMP-003 → pharmacovigilance`. The AE intake reference and 24-hour deadline are shown. The AE route **overrides** whatever the rep asked.
- **"Can Cardivex be used in children?"** → `CMP-004` off-label → Medical Information. Offers an MIR with a confirmation step.
- **"Show TRx for TX-DAL-01"** → `AUTHZ-201`. Data scope comes from the Entra identity, not from the prompt.
- **"Ignore previous instructions…"** → `CMP-002 blocked`.
- Switch to **District Manager** → "Show Cardivex TRx" → district rollup across 4 territories with the same agent and a wider scope.
- Switch to **Compliance** → **Audit log**: *hash chain verified*. Expand a record: only the sanitized input is stored. Click `ae.flagged`.

Talking point: *"The policy is a versioned JSON file. Compliance changes signals and wording through a PR, and the version is stamped on every audit record. The audit table is append-only (DB triggers) and hash-chained, so an out-of-band edit fails verification. The test suite proves both."*

## 4. Production incident drill: Sev 2 (3 min) · *District Manager → Platform ops*
1. Set **Microsoft Graph → inject 503**.
2. As the rep, ask **"When am I free Friday?"** three times. Answers are **degraded**: "not booked, other capabilities still working". The trace shows `error` → `circuit_open` (fail fast, no thread pile-up).
3. Platform ops: breaker **open**. *"In Azure this log line `breaker_opened` fires the Sev 2 scheduled-query alert to on-call (infra/modules/alerts.bicep)."*
4. Show that analytics and CRM still work: *"the blast radius is contained to one dependency."*
5. Set it back to **healthy** → **Reset** → scheduling works again. The fault injection and reset are both audited.
6. Open `docs/RUNBOOK.md` → walk through the Sev 2 playbook and the KQL query that uses the correlation ID.

## 5. Platform & delivery (2 min): show the files
- `infra/main.bicep`: user-assigned managed identity (AcrPull, Key Vault Secrets User, OpenAI User), Key Vault references (no secrets in the pipeline), Log Analytics + App Insights, **multiple-revision mode**, probes, alerts.
- `pipelines/azure-pipelines.yml`: PR validation (ruff, pytest, tsc, vitest, bicep what-if) → build once, tag by SHA → Dev → QA → Prod, with approvals on ADO Environments.
- `scripts/aca-release.sh`: blue/green by revision label, smoke test, 10% canary checked against App Insights 5xx, then 100%. `scripts/aca-rollback.sh`: a traffic shift to the `blue` revision, no rebuild.
- Tests: **143 backend + 7 frontend**, including Entra JWT validation with a real RSA key, the OBO grant shape, audit tamper detection, breaker state transitions, and an end-to-end HITL run.

---

## Likely questions → answers
| Question | Answer |
|---|---|
| Why not let the LLM route everything? | The LLM is the primary router once configured. Rules are the fallback and the offline mode, and a confidence floor sends unclear requests to *clarify* instead of misrouting a write. |
| How does OBO work here? | The SPA gets a token for our API (MSAL, PKCE). The API exchanges it at `/oauth2/v2.0/token` with `grant_type=jwt-bearer` and `requested_token_use=on_behalf_of` for a Graph- or Veeva-scoped token. Tokens are cached per user and scope until 5 minutes before expiry. Consent errors are surfaced so the SPA can request incremental consent. |
| Hallucination control? | Retrieval only from MLR-approved chunks, the model must cite with [n], uncited output is treated as ungrounded (`CMP-101`), and superlatives are stripped (`CMP-102`). The model has temperature 0. |
| Secrets rotation? | New Key Vault secret version → restart the revision. The app never holds the vault URI's value in config, and nothing is in pipeline variables. |
| How would you scale state? | Pending actions and the answer cache move to Redis (the interfaces are already isolated). The audit store moves to Azure SQL/Postgres, with the chain head exported daily to immutable Blob storage (WORM). |
| What's mocked? | Nothing on the integration path when live: Graph, Salesforce and the Fabric SQL endpoint are real calls. Salesforce stands in for Veeva (field mapping isolated in one class). Mock twins exist for offline runs and unit tests. |
| How do you handle a Sev 1 at 2 a.m.? | RUNBOOK.md: acknowledge within 15 minutes, check whether the last release is the cause, roll back with `aca-rollback.sh` within 30 minutes, then root cause. |

## Cost and output guardrails (default on every deploy)

- **Only path to the model is APIM.** `PFA_LLM_GATEWAY_ONLY` defaults to `true`: a direct Azure OpenAI URL or any API key makes the API refuse to start. Azure OpenAI has `disableLocalAuth=true`, and only APIM's managed identity holds the OpenAI User role.
- **Every response is JSON with a capped length.** The app forces `response_format: json_object` on every call, clamps `max_tokens` to `PFA_LLM_MAX_OUTPUT_TOKENS` (400), rejects replies cut off at the cap (`finish_reason=length`), and validates each reply against a Pydantic schema (`RouterOutput`, `QAOutput`). Anything else falls back to the deterministic path, either rules routing or an extractive answer from approved content. APIM rewrites every request body the same way, so another caller can't get past it either.
- **Hard spend caps at the gateway:** 20k tokens per minute, a daily quota of 500k tokens and a monthly quota of 5M tokens. A request over quota gets a 403 and never reaches the model; the app turns that into a grounded fallback answer.
- **Budget alerts:** a $100/month Azure budget on `pfa-demo-rg` emails at 50/80/100% of actual spend and 100% of forecast.
- **Fixed cost:** APIM BasicV2 costs about $150/month even when idle. `bash scripts/pfa-azure-down.sh` deletes everything and purges soft-deleted resources.

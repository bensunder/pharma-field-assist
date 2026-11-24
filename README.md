# PharmaFieldAssist

A governed, multi-agent AI assistant for a US pharmaceutical field sales force, built on **FastAPI, React/TypeScript and Azure Container Apps**.

Reps, district managers and compliance reviewers ask questions in plain English. Every turn runs the same pipeline:

1. Screen the message.
2. Route it to the right agent.
3. Check the user is allowed to use that agent.
4. Run the agent.
5. Screen the answer.
6. Write a tamper-evident audit record.

The model is reachable **only through an Azure API Management AI gateway**. That gateway enforces identity, token caps, JSON-only output, and daily and monthly quotas.

> All products (Cardivex, Respira HFA), HCPs and sales figures are fictional. The Graph, Salesforce and Fabric clients run live when configured (see [`docs/LIVE_SETUP.md`](docs/LIVE_SETUP.md)); with nothing configured, each one runs in mock mode.

## Start here

| If you are… | Read |
| --- | --- |
| A Chief AI Officer, executive or governance board | [Executive brief](docs/EXECUTIVE_BRIEF.md): the decision, controls, risk register, cost model, KPIs, operating model, roadmap |
| A principal or enterprise architect | [Architecture design](docs/ARCHITECTURE.md): drivers and quality scenarios, C4 views, runtime sequences, data and security architecture, STRIDE threat model, failure modes, capacity model, 12 ADRs, target state |
| An engineer or operator | This README, the [runbook](docs/RUNBOOK.md) and [live setup](docs/LIVE_SETUP.md) |

## At a glance

| | |
| --- | --- |
| **Business outcome** | Faster, compliant answers for field reps; off-label and adverse-event handling that never depends on a model's judgement |
| **Governance** | Controls mapped to the NIST AI RMF (Govern, Map, Measure, Manage); versioned compliance policy; tamper-evident audit of every turn |
| **Cost control** | One AI gateway: 400 output tokens per call, 20K tokens/min, 500K/day, 5M/month; answer cache; budget alerts |
| **Security** | Entra ID; On-Behalf-Of for user data; managed identities, no keys; internal-only API; Key Vault |
| **Resilience** | Circuit breakers and deterministic fallbacks: model outages degrade to approved-content answers, never errors |
| **Verified live on Azure** | Answer served through the gateway with keys disabled (426 prompt / 71 completion tokens); unauthorised gateway call rejected with 401; smoke tests on every release |
| **Quality** | 206 backend + 7 frontend tests; ruff and TypeScript clean |

## Screenshots

Live on Azure Container Apps, with every model call going through the API Management gateway.

**A grounded answer with its full agent trace.** Routed by the model (confidence 0.95), answered only from MLR-approved content with a citation, Important Safety Information appended by CMP-103, every compliance verdict shown, and the turn written to the audit chain.

![Grounded product answer with agent trace, compliance verdicts and audit record](docs/images/02-grounded-answer-agent-trace.png)

**Compliance view of the audit log.** The hash chain is verified, and every turn is recorded with its actor, route, answer, citations and versions.

![Compliance audit log with verified hash chain and full turn record](docs/images/03-compliance-audit-log.png)

**Platform operations.** Shows the environment, the release version, the model path (`azure-openai via APIM gateway: gpt-4.1-mini`), answer-cache hits, and circuit-breaker state and fault injection per dependency. It also shows the knowledge base with an expired document excluded by MLR gating.

![Platform ops: model via APIM gateway, dependency breakers, knowledge base with expired content excluded](docs/images/04-platform-ops-knowledge-base.png)

<details>
<summary>More screens: home and district-manager view</summary>

![Field assistant home with persona switch and suggested questions](docs/images/01-field-assistant-home.png)

![District manager persona with district-wide scope](docs/images/05-district-manager-platform-ops.png)

</details>

---

## Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Request pipeline](#request-pipeline)
4. [Agents](#agents)
5. [Compliance engine](#compliance-engine)
6. [Knowledge pipeline](#knowledge-pipeline)
7. [LLM governance: gateway only, JSON only, capped spend](#llm-governance-gateway-only-json-only-capped-spend)
8. [Security and identity](#security-and-identity)
9. [Integrations](#integrations)
10. [Resilience and observability](#resilience-and-observability)
11. [Audit log](#audit-log)
12. [Deployment](#deployment)
13. [Configuration](#configuration)
14. [API](#api)
15. [Testing](#testing)
16. [Repository layout](#repository-layout)
17. [Design decisions and trade-offs](#design-decisions-and-trade-offs)
18. [Limitations and roadmap](#limitations-and-roadmap)

---

## What it does

| Capability | Example question | What happens |
| --- | --- | --- |
| Product Q&A | "What is the starting dose of Cardivex?" | Answer from MLR-approved content, with a citation and the product's Important Safety Information appended |
| Scheduling | "Schedule a meeting with Dr. Patel tomorrow at 2pm" | Checks the Outlook calendar, then waits for the user to click Confirm before booking |
| Analytics | "Show my Cardivex TRx for the last 8 weeks" | Territory-scoped weekly prescription data from Microsoft Fabric |
| CRM | "Log a call with Dr. Okafor: discussed formulary coverage" | Writes a call record to Salesforce/Veeva after confirmation |
| Adverse events | "Her patient had muscle pain after starting Cardivex" | Routed to pharmacovigilance with an intake reference; the patient name is redacted |
| Off-label | "Can Cardivex be used in children?" | Not answered; routed to Medical Information |
| Prompt injection | "Ignore previous instructions…" | Blocked before any model call |

Three demo personas exercise role- and territory-based access:

| Persona | Role | Data scope |
| --- | --- | --- |
| Jordan Reyes | `FieldRep` | Territory `TX-HOU-03` |
| Priya Natarajan | `DistrictManager` | District `TX-HOU` (all its territories) |
| Marcus Lee | `ComplianceOfficer` | Product Q&A and the audit log only |

---

## Architecture

```mermaid
flowchart LR
  subgraph Client
    B[Browser<br/>React SPA, MSAL]
  end
  subgraph ACA["Azure Container Apps environment"]
    W["Web app (public)<br/>nginx: CSP, HTTP/1.1,<br/>X-PFA-Token to Bearer"]
    A["API app (internal ingress)<br/>FastAPI orchestrator"]
  end
  subgraph Platform
    G["API Management<br/>AI gateway"]
    O["Azure OpenAI<br/>keys disabled"]
    KV[Key Vault]
    AI[App Insights<br/>Log Analytics]
  end
  subgraph Data["User data (On-Behalf-Of)"]
    GR[Microsoft Graph<br/>Outlook]
    SF[Salesforce / Veeva]
    FB[Microsoft Fabric<br/>SQL endpoint]
  end
  B -->|HTTPS| W --> A
  A -->|managed identity token| G -->|managed identity| O
  A --> GR & SF & FB
  A -.secrets.-> KV
  A -.telemetry.-> AI
  G -.token metrics.-> AI
```

**Principles behind the shape:**

- **Two identities, never mixed.** User data (calendar, CRM) is reached with the signed-in user's delegated token via On-Behalf-Of, so CRM row visibility and mailbox scoping stay the user's. Platform calls (model, Key Vault, registry) use the app's user-assigned managed identity.
- **One path to the model.** Only the API's managed identity can pass the gateway, and only the gateway's managed identity can call Azure OpenAI. Keys are disabled at both hops.
- **The API isn't on the internet.** It has internal ingress only; the web app is the single public entry point.
- **Agents are pure domain logic.** HTTP, auth, audit and telemetry live in the orchestrator and the `AgentContext`, so agents can be tested in isolation and later mapped one-to-one onto LangGraph nodes.

---

## Request pipeline

`Orchestrator.handle_turn` (`backend/app/orchestrator.py`) is the only place that composes guardrails, routing, authorization, agents and audit. Every step is timed and returned in the turn's trace.

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant O as Orchestrator
  participant IG as Input guardrails
  participant R as Router
  participant Z as AuthZ
  participant Ag as Agent
  participant OG as Output guardrails
  participant AU as Audit log
  U->>O: message (max 2,000 chars)
  O->>IG: CMP-001 PII, CMP-002 injection, CMP-003 AE, CMP-004 off-label
  alt blocked or forced route
    IG-->>O: blocked, or route to pharmacovigilance / medical_information
  else normal
    O->>R: classify (LLM via APIM, JSON schema)
    R-->>O: intent + confidence, or rules fallback, or clarify if under 0.35
  end
  O->>Z: AUTHZ-101 role x agent
  O->>Ag: run with AgentContext (tool calls traced, breaker-wrapped)
  Ag-->>O: result, citations, optional pending action
  O->>OG: CMP-101 grounding, CMP-102 prohibited claims, CMP-103 fair balance
  O->>AU: append hash-chained record
  O-->>U: answer + trace + audit seq/hash
```

**Human in the loop for writes.** Calendar bookings, CRM call logs and Medical Information requests come back as a *pending action* with a summary. Nothing is written until the user confirms. Pending actions are bound to the user who created them and expire after 15 minutes. Graph event creation uses the action ID as `transactionId`, so a retried confirm never double-books.

---

## Agents

| Agent | Responsibility | Tools and identity | Writes |
| --- | --- | --- | --- |
| `router` | Classifies into `product_qa`, `scheduling`, `analytics`, `crm`, `help`. The LLM returns `RouterOutput` JSON; weighted keyword rules are the fallback; below 0.35 confidence the turn becomes `clarify` | APIM gateway (managed identity) | — |
| `product_qa` | Grounded answers with numbered citations. An answer cache keyed on normalised question, product and knowledge-base version serves repeats with no model call | `knowledge.search` (in-process index), `llm.complete` (managed identity → APIM → Azure OpenAI) | — |
| `scheduling` | Free/busy, conflict check, booking | Microsoft Graph `calendarView` / `events` (OBO) | `calendar.create_event` (confirm) |
| `analytics` | TRx, NRx and market share by week from a whitelisted query spec; never free-form SQL | Fabric SQL endpoint; AUTHZ-201 territory scope | — |
| `crm` | Account lookup and call logging in the caller's territory | Salesforce/Veeva REST + SOQL (OBO or JWT bearer) | `crm.log_call` (confirm) |
| `pharmacovigilance` | Adverse-event intake reference, the reporting window, the four minimum reportable criteria | Forced by CMP-003; audit event `ae.flagged` | — |
| `medical_information` | Turns an off-label question into a Medical Information Request | Forced by CMP-004 | `medinfo.submit_mir` (confirm) |

Every tool call goes through `AgentContext.call_tool`. That records the tool, dependency, identity (with a token fingerprint, never the token), status, latency and result summary, and wraps the call in the dependency's circuit breaker.

---

## Compliance engine

Rules are **data**, not code: `backend/data/compliance_policy.json` (version `2026.11.2`), validated by a Pydantic `Policy` model at startup, so a malformed policy fails to boot instead of silently passing everything. Rule *logic* lives in `backend/app/compliance/`. Every verdict carries the rule ID, stage and outcome (`pass` · `amended` · `routed` · `blocked`), and appears in both the trace and the audit record.

| Rule | Stage | Behaviour |
| --- | --- | --- |
| CMP-001 | Input | Redacts SSN, email, phone, MRN, DOB and patient names before routing, model calls or logging |
| CMP-002 | Input | Blocks prompt-injection patterns ("ignore previous instructions", "system prompt", "developer mode") |
| CMP-003 | Input | Adverse-event detection. Strong signals fire alone; weak signals ("side effect", "rash") need patient context, so general safety questions aren't misrouted. Forces the pharmacovigilance agent |
| CMP-004 | Input | Off-label signals (pediatric, pregnancy, unapproved indications, dose escalation). Forces Medical Information |
| AUTHZ-101 | Authz | Role × agent matrix |
| AUTHZ-201 | Agent | Territory and district scope on analytics and CRM data |
| CMP-101 | Output | Product content must cite an MLR-approved chunk, or it's replaced with a Medical Information referral |
| CMP-102 | Output | Removes sentences with prohibited claims ("safest", "cure", "no side effects", "superior to") |
| CMP-103 | Output | Appends Important Safety Information when product content is shown (fair balance) |

A compliance block is a business outcome, not an error. It returns HTTP 200 with a `blocked`/`routed` verdict and goes to the audit log, so it never pages on-call.

---

## Knowledge pipeline

`backend/app/knowledge/index.py` builds an in-memory index from `backend/knowledge/*.md` at startup.

1. **Ingest.** Front matter per document: `title`, `product`, `mlr_id`, `approved_until`, `audience`.
2. **Govern.** Documents without an MLR ID, or past `approved_until`, are excluded *at index time*. `cardivex-launch-messaging-2025.md` ships expired on purpose to prove it.
3. **Chunk.** Split on H2/H3 headings; every chunk keeps title, section and MLR ID for its citation.
4. **Retrieve.** BM25 with a title/section boost, a suffix stemmer, query synonym expansion, and a non-topical filter (a chunk can't match on the product name alone). Top 3 (`PFA_RETRIEVAL_TOP_K`), minimum score 0.8, and a relative cutoff at 60% of the best hit.
5. **Version.** A content hash is the knowledge-base version. It's stamped on every audit record and is part of the answer-cache key, so a content release invalidates cached answers.

BM25 was chosen over vectors for this corpus because it's deterministic and explainable to MLR reviewers. Agents depend only on the `KnowledgeIndex.search()` contract, so Azure AI Search hybrid retrieval can replace it without touching agents.

---

## LLM governance: gateway only, JSON only, capped spend

Each control is enforced in more than one place, so one misconfiguration can't open a direct path or uncap spend.

**Single path to the model**

- `PFA_LLM_GATEWAY_ONLY` defaults to `true`. The API refuses to start if the endpoint isn't `*.azure-api.net` or if any API key is set. A direct endpoint needs an explicit opt-out (local experiments only).
- `PFA_LLM_REQUIRED=true` in Azure, so a missing gateway URL is a startup failure, not a silent "no model" mode.
- Azure OpenAI runs with `disableLocalAuth=true`. Only APIM's managed identity holds *Cognitive Services OpenAI User*.
- APIM `validate-azure-ad-token` accepts only the API's managed-identity client ID (401 `TokenNotPresent` otherwise).

**Structured, capped output**

- **In the app:** every model call goes through `complete_json()` (`backend/app/llm/provider.py`). It forces `response_format: json_object`, clamps `max_tokens` to `PFA_LLM_MAX_OUTPUT_TOKENS` (400), rejects replies cut off at the cap (`finish_reason=length`), and validates the reply against a Pydantic schema (`RouterOutput`, `QAOutput`). Anything else raises, and the caller falls back to rules routing or an extractive answer. Free text never reaches a user.
- **At the gateway:** a `set-body` policy rewrites every request: `max_tokens = min(asked, 400)`, `max_completion_tokens` and `stream` removed, `response_format` forced to `json_object`.

**Spend caps**

| Control | Limit | Past the limit |
| --- | --- | --- |
| APIM `azure-openai-token-limit` | 20,000 tokens per minute | 429; the app falls back |
| APIM daily token quota | 500,000 tokens per UTC day | 403; the app falls back, logs `llm_quota_exhausted` |
| APIM monthly token quota | 5,000,000 tokens per month | 403; same |
| Deployment capacity | 10K TPM GlobalStandard | Hard ceiling at the model |
| Answer cache | Repeat questions per KB version | No model call |
| Azure budget | $100/month on the resource group | Email at 50/80/100% actual and 100% forecast |

**Metering:** `azure-openai-emit-token-metric` sends prompt, completion and total tokens to App Insights (namespace `pfa-llm`), measured at the gateway rather than self-reported by the app.

**Model selection:** the deploy script lists what the Azure OpenAI account can deploy now and picks the first current, non-deprecating model from `gpt-4.1-mini, gpt-4o-mini, gpt-4.1-nano, gpt-4.1, gpt-4o`. Reasoning models are excluded because they ignore `max_tokens` and `temperature=0`, which the caps and deterministic routing rely on.

---

## Security and identity

| Concern | Design |
| --- | --- |
| User sign-in | `PFA_AUTH_MODE=entra`: MSAL.js (authorization code + PKCE). The API validates signature (tenant JWKS), issuer, audience and expiry, and reads `oid`, app `roles` and a `territory` claim. `demo` mode issues short-lived HS256 tokens for the three personas; the signing key is in Key Vault |
| Delegated access | On-Behalf-Of exchange (`backend/app/security/obo.py`) for Graph and Salesforce, so downstream visibility is the user's own |
| Service identity | One user-assigned managed identity: AcrPull, Key Vault Secrets User, and the token audience for the APIM gateway |
| Secrets | Key Vault (RBAC mode), referenced by Container Apps as `keyvaultref` secrets; never in images, config or code |
| Edge | The API has internal ingress only. nginx on the web app sets `X-Frame-Options: DENY` and a CSP limited to self and `login.microsoftonline.com`; demo deployments add basic auth |
| Token transport | The SPA sends its token in `X-PFA-Token`; nginx maps it to `Authorization: Bearer` upstream, so an edge `Authorization` (basic auth) never collides with the app token |
| Injection-safe data access | SOQL values escaped with `soql_literal`; Fabric queries use whitelisted metrics/tables and bound parameters only |

---

## Integrations

Each integration has a live client and a mock twin behind one interface, selected by a mode flag. Live mode with missing config fails at startup.

| Integration | Live client | Calls | Flag |
| --- | --- | --- | --- |
| Microsoft Graph / Outlook | `GraphCalendarClient` | `GET /calendarView` (with `Prefer: outlook.timezone`), `POST /events` with `transactionId` | `PFA_GRAPH_MODE` |
| Salesforce / Veeva | `SalesforceCrmClient` | SOQL on `Account` by territory, `POST /sobjects/Task`; client credentials or JWT bearer | `PFA_CRM_MODE` |
| Microsoft Fabric | `FabricSqlClient` (`mssql-python`) | Parameterised T-SQL on the Lakehouse SQL endpoint, Entra token provider, no SQL password | `PFA_FABRIC_MODE` |

`http_errors.py` maps 401/403, 404, 429 and 5xx to clear dependency errors. `backend/scripts/check_live.py` prints PASS/FAIL per integration; `backend/scripts/seed_salesforce.py` seeds demo accounts.

---

## Resilience and observability

- **Circuit breakers** (`backend/app/resilience/breaker.py`) wrap Graph, CRM, Fabric and the LLM. A breaker opens after 3 consecutive failures, goes half-open after 30 seconds, and closes on 1 success. While open, calls fail fast and agents degrade (product Q&A falls back to an extractive answer).
- **Fault injection** (`backend/app/resilience/faults.py`): `error` (503) or `timeout` per dependency, from the Platform ops panel. Allowed in dev; forced off in `prod` by a settings validator.
- **Throttling drill:** `scripts/pfa-throttle-drill.sh on|off` lowers the APIM per-minute limit to 1,000 so real 429s appear at the gateway and in App Insights while users still get compliant answers.
- **Telemetry:**
    - Azure Monitor OpenTelemetry sends requests, logs and exceptions to App Insights (service name via `OTEL_SERVICE_NAME`).
    - Structured JSON logs carry a correlation ID, which is also returned as `X-Correlation-ID`.
    - Named events: `agent_failed`, `router_llm_fallback`, `llm_output_rejected`, `llm_quota_exhausted`, `llm_auth_rejected`, `breaker_opened`, `breaker_closed`, `obo_exchange_failed`, `fabric_query_failed`.
    - Azure SDK and HTTP-client loggers are held at WARNING to avoid a telemetry feedback loop.
- **Health:** `/health/live` always answers; `/health/ready` returns 503 unless the audit store answers and the knowledge index has chunks. Both are open through the edge for probes.
- **Ops status:** `/api/ops/status` reports app, policy and KB versions, breaker states, faults, cache hit rate, and live/mock mode per integration.
- **Incident support:** [`docs/RUNBOOK.md`](docs/RUNBOOK.md) has Sev 1–4 definitions with acknowledge/mitigate/comms targets, first-10-minutes steps, playbooks (5xx spike, breaker open, LLM throttling, suspected compliance failure, secret rotation), and blameless RCAs with a regression test per fix. A compliance control failing open is always Sev 1.

---

## Audit log

`backend/app/audit/store.py` is an append-only, hash-chained log.

- Each record stores `sha256(prev_hash + canonical_json(record))`, so editing any record breaks the chain from that point. `GET /api/audit/verify` walks the chain and reports the first break.
- SQLite triggers reject `UPDATE` and `DELETE`.
- Each record carries the correlation ID, user, role, route, verdicts, tool calls, policy version and KB version. Only the sanitized message is stored.
- Production target: Azure SQL ledger tables or Blob storage with a time-based retention lock, with the chain head exported regularly.

---

## Deployment

### Azure (what's running)

Idempotent shell scripts, safe to re-run. They're ASCII-only and bash 3.2 compatible, so they run on stock macOS with the Azure CLI and Python from `uv` (no Homebrew).

| Script | Purpose |
| --- | --- |
| `scripts/pfa-azure.sh` | Whole environment from zero: providers, resource group, Log Analytics + App Insights, ACR, managed identity, Key Vault, image builds in ACR, Container Apps (API internal, web public), smoke test. Calls `pfa-apim.sh` unless `SKIP_APIM=1` (which means no model, never a direct path) |
| `scripts/pfa-apim.sh` | Azure OpenAI (quota-checked across regions, keys disabled), model selection, APIM BasicV2, role grant, gateway API and policies, API switched to gateway-only, budget, end-to-end verification |
| `scripts/pfa-azure-web.sh` | Rebuild and roll the web app only; waits for the new revision and smoke-tests it |
| `scripts/aca-release.sh` / `aca-rollback.sh` | Blue/green for the API (multiple-revision mode): new revision at 0%, smoke test on its label URL, 10% canary while watching 5xx, then 100%; previous revision labelled for instant rollback |
| `scripts/pfa-throttle-drill.sh` | Gateway throttling drill on/off |
| `scripts/pfa-azure-down.sh` | Delete everything and purge soft-deleted Key Vault, Azure OpenAI and APIM |
| `scripts/smoke.sh` | Health, demo token, a real chat turn, audit sequence check |

Resources created: Container Apps environment, `ca-pfa-api` (internal, 0.5 vCPU / 1 GiB), `ca-pfa-web` (public), ACR, user-assigned identity, Key Vault, Log Analytics, App Insights, Azure OpenAI, API Management BasicV2, and a budget.

> **Cost:** APIM BasicV2 bills about $150/month even when idle. Run `scripts/pfa-azure-down.sh` when you're done.

### Azure DevOps and Bicep

- `pipelines/azure-pipelines.yml`:
    - **Validate:** ruff + pytest; tsc + vitest + build; Bicep build + what-if.
    - **Build** (main only): ACR build tagged by commit SHA, then a Trivy scan that fails on CRITICAL.
    - **Deploy** dev → QA → prod from `templates/deploy-env.yml`: Bicep pinned to the live image, blue/green + canary API release, web release, automatic rollback. QA and prod approvals live on ADO Environments (QA lead; CAB + Compliance in business hours).
- `infra/main.bicep` + modules: identity, Key Vault, monitoring, Container Apps, and alert rules (5xx rate, breaker opened, chat p95 latency, restarts). Per-environment `.bicepparam` files.

### Local and VPS

```bash
docker compose up --build        # UI http://localhost:8080 · API docs http://localhost:8000/docs
scripts/dev.sh                   # without Docker, UI http://localhost:5173
bash scripts/pfa-vps.sh          # on a Linux VPS from the project folder: public web on :8711 behind basic auth
```

---

## Configuration

Environment variables, prefix `PFA_` (see `backend/app/config.py`):

| Variable | Default | Notes |
| --- | --- | --- |
| `PFA_ENVIRONMENT` | `local` | `prod` disables `/docs` and fault injection |
| `PFA_AUTH_MODE` | `demo` | `entra` validates Entra ID tokens and disables demo tokens |
| `PFA_ENTRA_TENANT_ID`, `PFA_ENTRA_AUDIENCE`, `PFA_ENTRA_API_CLIENT_ID`, `PFA_ENTRA_API_CLIENT_SECRET` | — | Secret via Key Vault reference |
| `PFA_AZURE_OPENAI_ENDPOINT` | — | Must be the APIM gateway URL when gateway-only is on. Unset = no model (extractive answers, zero spend) |
| `PFA_AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` | Overridden by `pfa-apim.sh` with the model it deployed |
| `PFA_LLM_GATEWAY_ONLY` | `true` | Refuse non-gateway endpoints and any API key |
| `PFA_LLM_REQUIRED` | `false` | `true` = startup fails without a model endpoint |
| `PFA_LLM_MAX_OUTPUT_TOKENS` | `400` | Ceiling on every model call (16–4000) |
| `PFA_RETRIEVAL_TOP_K` / `PFA_RETRIEVAL_MIN_SCORE` | `3` / `0.8` | Retrieval tuning |
| `PFA_APPLICATIONINSIGHTS_CONNECTION_STRING` | — | Enables Azure Monitor OpenTelemetry |
| `PFA_GRAPH_MODE`, `PFA_CRM_MODE`, `PFA_FABRIC_MODE` | `mock` | `live` = real Graph / Salesforce / Fabric |
| `PFA_INTEGRATION_CLIENT_ID` / `PFA_INTEGRATION_CLIENT_SECRET` | — | App-only identity (Graph in demo mode, Fabric) |
| `PFA_GRAPH_DEMO_MAILBOX` / `PFA_GRAPH_MAILBOX_MAP` | — | Mailboxes the demo personas book into |
| `PFA_SF_LOGIN_URL`, `PFA_SF_CLIENT_ID`, `PFA_SF_CLIENT_SECRET`, `PFA_SF_AUTH` | `client_credentials` | `jwt_bearer` with `PFA_SF_JWT_PRIVATE_KEY_PATH` acts as each rep |
| `PFA_FABRIC_SQL_ENDPOINT`, `PFA_FABRIC_DATABASE`, `PFA_FABRIC_TABLE` | `weekly_rx_by_territory` | Lakehouse SQL endpoint |
| `AZURE_CLIENT_ID` | — | User-assigned managed identity client ID |
| `OTEL_SERVICE_NAME` | — | Service name shown on the App Insights Application map |

Frontend build args: `VITE_AUTH_MODE`, `VITE_ENTRA_CLIENT_ID`, `VITE_ENTRA_TENANT_ID`, `VITE_API_SCOPE`.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/auth/demo-token` | Demo persona token (demo mode only) |
| `GET` | `/api/auth/personas`, `/api/me` | Persona list; the caller's principal |
| `POST` | `/api/chat` | Run one turn; returns answer, citations, trace, verdicts, audit seq/hash, optional pending action |
| `POST` | `/api/actions/{id}/confirm` · `/cancel` | Human-in-the-loop execution of a pending write |
| `GET` | `/api/audit` · `/api/audit/verify` | Audit events (filterable); chain verification |
| `GET` | `/api/ops/status` | Versions, breakers, faults, cache, integration modes |
| `POST` | `/api/ops/faults` · `/api/ops/breakers/{name}/reset` | Fault injection and breaker reset (non-prod) |
| `GET` | `/health/live` · `/health/ready` | Probes |

The API accepts the app token as `Authorization: Bearer <token>` or `X-PFA-Token: <token>`. OpenAPI docs are at `/docs` outside prod.

---

## Testing

```bash
cd backend  && ruff check . && ruff format --check . && pytest     # 206 tests
cd frontend && npm run typecheck && npm test && npm run build       # 7 tests
```

| Suite | Tests | Covers |
| --- | --- | --- |
| `test_router_and_parsers.py` | 45 | LLM routing, rules fallback, confidence floor; day/time/duration parsing; analytics parsing; territory scope |
| `test_api.py` | 43 | Full turns over HTTP for every persona; HITL confirm/cancel/expiry; cache; schema-invalid model output never reaching users; token budgets; `X-PFA-Token` |
| `test_live_integrations.py` | 29 | Graph, Salesforce and Fabric live clients against simulated API responses: auth, paging, idempotency, SOQL escaping, error mapping |
| `test_llm_gateway.py` | 26 | Gateway-only enforcement, key refusal, managed-identity tokens, JSON mode on every call, `max_tokens` clamping, truncation rejection, quota 403s |
| `test_compliance.py` | 24 | Every CMP rule, including AE strong vs weak signals |
| `test_knowledge_audit_breaker.py` | 22 | MLR expiry gating, retrieval quality, hash-chain tamper detection, breaker state machine |
| `test_security_integrations.py` | 17 | Entra token validation, OBO exchange and caching, consent errors, Azure OpenAI adapter status handling |
| `frontend/src/lib/markdown.test.tsx` | 7 | Answer rendering (including snake_case identifiers) |

---

## Repository layout

```
backend/
  app/
    orchestrator.py        turn pipeline + HITL execution
    agents/                router, product_qa, scheduling, analytics, crm_agent, safety (PV + MedInfo), base (AgentContext)
    compliance/            guardrails, PII scrubber, Pydantic policy model
    knowledge/             Markdown ingestion, MLR gating, chunking, BM25
    llm/provider.py        APIM-only Azure OpenAI provider, complete_json(), managed-identity tokens
    security/              Entra JWT validation, demo personas, OBO broker, app tokens
    integrations/          Graph, Salesforce, Fabric (live + mock), HTTP error mapping
    resilience/            circuit breaker, fault injection
    audit/store.py         hash-chained append-only store
    api/routes.py          REST API and health
    core/                  logging, errors
    config.py              settings (PFA_*) with fail-fast validation
  data/                    compliance_policy.json, mock CRM/Fabric data
  knowledge/               MLR-approved Markdown corpus (one expired on purpose)
  scripts/                 check_live.py, seed_salesforce.py
  tests/
frontend/                  React + TypeScript (Vite): chat, agent trace, ops, audit; nginx config
infra/                     Bicep main + modules + per-environment params
pipelines/                 Azure DevOps YAML + deploy template
scripts/                   Azure, APIM, web, release/rollback, drill, teardown, VPS, smoke, dev
docs/                      DEMO_SCRIPT.md, RUNBOOK.md, LIVE_SETUP.md
```

---

## Design decisions and trade-offs

| Decision | Chosen | Rejected, and why |
| --- | --- | --- |
| LLM access | One APIM gateway, managed identity only | Direct SDK calls with keys: no central quota, metering or audit; keys spread across services |
| Output contract | JSON schema per call plus deterministic fallbacks | Free text: impossible to validate, and unsafe to show in a regulated domain |
| Routing | LLM classifier with rules fallback and a confidence floor | LLM-only: a model outage would stop the app. Rules-only: brittle on paraphrase |
| Compliance rules | Policy-as-data (versioned JSON + Pydantic) with logic in code | Rules hard-coded: every wording change becomes a code release with no clear policy version |
| Retrieval | BM25 + synonyms for a small, approved corpus | Vectors first: less explainable to MLR reviewers, with no measured quality gain at this size. Hybrid via Azure AI Search is the scale path |
| Writes | Human confirmation, user-bound, time-limited, idempotent | Autonomous actions: unacceptable for CRM records and HCP interactions in pharma |
| Orchestration | Plain async pipeline shaped as graph nodes | A framework from day one: more moving parts before the flow was proven; maps onto LangGraph later |
| Audit | Hash chain + DB triggers | Plain logs: no tamper evidence |
| Release | Multiple-revision Container Apps with canary and labels | In-place updates: no instant rollback |

---

## Limitations and roadmap

| Today | Next |
| --- | --- |
| Integrations run on mock data in the hosted demo | Connect a tenant mailbox, Salesforce org and Fabric workspace; run `check_live.py` |
| Demo personas | `PFA_AUTH_MODE=entra` with app roles and a `territory` claim |
| SQLite audit, in-memory pending actions and cache, single API replica | Azure SQL ledger / immutable Blob; Redis; scale out |
| Rule-based injection, AE and off-label detection | Add Azure AI Content Safety Prompt Shields and classifiers, keeping rules as the floor |
| BM25 over 7 documents | Azure AI Search hybrid + semantic ranker behind the same `search()` contract, fed from the MLR system of record |
| APIM and Azure OpenAI created by script | Add them to Bicep and run the ADO pipeline end to end |
| No RAG evaluation gate | Golden question set scoring retrieval recall and groundedness as a PR gate |
| Model-call dependencies not yet traced from the API side | Add `opentelemetry-instrumentation-httpx` and a per-turn `turn_completed` event tagged by agent |
| Public endpoints with edge basic auth | Front Door/App Gateway with WAF; private endpoints for Key Vault, Azure OpenAI, APIM |

See also: [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) · [`docs/RUNBOOK.md`](docs/RUNBOOK.md) · [`docs/LIVE_SETUP.md`](docs/LIVE_SETUP.md)

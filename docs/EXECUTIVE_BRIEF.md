# PharmaFieldAssist — Executive Brief

**For:** Chief AI Officer, AI governance board, Compliance, IT leadership
**Status:** Working platform deployed on Azure (gateway, model, identity, secrets, monitoring live); calendar, CRM and analytics integrations on demo data; production path defined below.

---

## 1. The decision this brief supports

Should the organization take an AI assistant for field sales into a controlled pilot? The platform is built to answer the questions that usually block that decision:

| Leadership question | Short answer |
| --- | --- |
| Will it say something non-compliant? | Answers come only from MLR-approved content, carry a citation and safety information, and are checked after generation. Off-label questions and adverse events are routed to Medical Information and Pharmacovigilance, never answered by the model. |
| Can spend run away? | No. The model is reachable only through one gateway that caps every call (400 output tokens), the rate (20,000 tokens/min), the day (500,000) and the month (5,000,000). Over the cap, requests are refused and users still get an approved-content answer. |
| Can we prove what it did? | Every interaction is written to a tamper-evident, hash-chained audit log: who asked, what was retrieved, which rules fired, what was answered, under which policy and content version. |
| Will it expose data people shouldn't see? | Calendar and CRM data are accessed with the user's own identity, and analytics are restricted to the user's territory. Platform services use managed identities with no keys. |
| What happens when something fails? | Each dependency has a circuit breaker and a fallback. A model outage or quota hit degrades to approved-content answers instead of failing. |

**What a rep sees, and what Compliance can prove.** Every answer carries its approved source and safety information, and the trace beside it shows each step and rule outcome:

![Grounded product answer with agent trace](images/02-grounded-answer-agent-trace.png)

![Audit log with verified hash chain](images/03-compliance-audit-log.png)

---

## 2. The business problem

Field representatives need fast, correct answers about products, coverage, scheduling and their territory. Today that means searching approved materials, switching between Outlook, Veeva/Salesforce and reporting tools, and occasionally improvising answers to questions they shouldn't answer at all. The commercial cost is time and missed calls. The regulatory cost is worse: an off-label statement, a missed adverse-event report, or a claim without fair balance creates real compliance exposure.

## 3. What the platform does

One conversational assistant, with six specialized agents behind it:

- **Product Q&A:** answers only from MLR-approved documents, with citations and Important Safety Information appended.
- **Scheduling:** checks the Outlook calendar and books HCP meetings after the user confirms.
- **Territory analytics:** prescription trends from Microsoft Fabric, scoped to the user's territory.
- **CRM:** logs calls in Veeva/Salesforce after confirmation.
- **Pharmacovigilance:** recognizes adverse events, opens an intake reference and states the reporting window.
- **Medical Information:** turns off-label questions into Medical Information requests instead of answering them.

Nothing that writes to a business system happens without the user's explicit confirmation.

---

## 4. Responsible AI and governance

The controls are designed to map onto the NIST AI Risk Management Framework, so the governance board can review them in a familiar structure. This is a design mapping, not a certification.

| NIST AI RMF function | How the platform addresses it |
| --- | --- |
| **Govern** | Compliance rules are versioned data owned by Compliance (`compliance_policy.json`), not hidden in code. Every audit record carries the policy and content versions in force. Release approvals for QA and production (QA lead; CAB and Compliance) are defined in the Azure DevOps delivery pipeline. |
| **Map** | Each agent has a defined purpose, role-based access, data scope and tool list. High-risk intents (adverse events, off-label) are identified before the model runs and routed to the accountable function. |
| **Measure** | 206 automated tests cover every compliance rule, schema-validated model output, token limits and failure modes. Token usage is metered at the gateway. The next step is a scored evaluation set for answer groundedness as a release gate. |
| **Manage** | Circuit breakers, fallbacks, spend quotas, blue/green releases with one-step rollback, a Sev 1–4 runbook where any compliance control failing open is Sev 1, and a scripted failure drill (gateway throttling). |

**Human oversight:** the model never takes an action on its own. It drafts; the user confirms. Clinical-safety and off-label decisions stay with Pharmacovigilance and Medical Information.

**Transparency:** every answer shows its source document and approval ID. The agent trace shows each step, tool and rule outcome behind an answer.

---

## 5. Cost control and economics

| Item | Control | Order of magnitude |
| --- | --- | --- |
| Model usage | Gateway quotas: 400 output tokens per call, 20K/min, 500K/day, 5M/month; answer cache for repeat questions | At the configured monthly cap, model spend is bounded to single-digit dollars per month on a small model at list prices |
| Per answer | Measured live: 426 prompt and 71 completion tokens for a cited product answer | A fraction of a cent per answer |
| Fixed platform | API Management BasicV2, Container Apps, Key Vault, monitoring | Roughly $150–200 per month, dominated by API Management |
| Oversight | $100/month budget with alerts at 50, 80 and 100% of actual spend and 100% of forecast | Alerting only; quotas are the hard stop |

Quotas are configuration, not code: raising them for a pilot is a reviewed change, metered per product at the gateway.

---

## 6. Risk register

| Risk | Impact | Mitigation in place | Residual / next step |
| --- | --- | --- | --- |
| Off-label or unapproved claim | High | Retrieval limited to approved content; off-label routing; prohibited-claims filter; fair balance appended | Add a scored groundedness evaluation as a release gate |
| Missed adverse event | High | Strong and weak AE signal detection with context; forced routing; audit event; treated as Sev 1 if it fails | Add an ML classifier alongside the rules; periodic PV review of samples |
| Patient data in prompts or logs | High | PII/PHI redaction before routing, model calls and logging; only sanitized text stored | Add Azure AI Content Safety and Purview labels on content |
| Unauthorized data access | High | User-delegated access for CRM and calendar; territory scoping; role × agent matrix; deny by default | Entra ID sign-in with app roles for production |
| Runaway model cost | Medium | Gateway-only access, per-call, per-minute, daily and monthly caps; budget alerts | Per-team quotas as usage grows |
| Prompt injection | Medium | Injection patterns blocked before routing; no model-initiated actions | Prompt Shields for user input and documents |
| Model deprecation or outage | Medium | Deploy-time selection of a current model; fallbacks; breaker | Second deployment behind the gateway for failover |
| Audit tampering | Medium | Hash chain plus database-level update/delete blocking | Immutable storage (ledger tables or locked Blob) for production |

---

## 7. How value will be measured in a pilot

| KPI | What it shows |
| --- | --- |
| Weekly active reps and questions per rep | Adoption |
| Median time to a cited answer | Productivity versus manual search |
| Share of answers with an approved citation | Compliance quality |
| AE and off-label questions correctly routed | Safety controls working in practice |
| Medical Information requests created through the assistant | Correct escalation instead of improvisation |
| Thumbs-down rate and reviewer findings | Answer quality as users see it |
| Model cost per answer and per active rep | Unit economics |

---

## 8. Operating model

| Role | Owns |
| --- | --- |
| AI product owner | Use-case backlog, adoption KPIs, pilot scope |
| AI platform team | Gateway, models, orchestration, reliability, releases |
| Compliance and MLR | Compliance policy versions, approved content, release sign-off |
| Pharmacovigilance and Medical Information | AE and off-label handling rules, periodic sample review |
| Security and identity | Access model, secrets, network controls |

Changes to prompts, models, compliance rules or approved content follow the same path: version, test, approve, release, with the versions recorded in every audit record.

---

## 9. Roadmap

1. **Pilot readiness:** Entra ID sign-in; live Outlook, Veeva and Fabric for one district; scored evaluation set as a release gate; private networking.
2. **Controlled pilot:** one district, KPIs baselined, weekly Compliance and PV sample review.
3. **Scale:** Azure AI Search hybrid retrieval fed from the MLR system of record; shared state for multiple replicas; immutable audit storage; second model deployment for failover.
4. **Extend:** new agents (call planning, HCP insights) on the same platform, each inheriting the same controls.

---

## 10. Evidence: what was verified live on Azure

- A model answer served end to end through the API Management gateway, with Azure OpenAI keys disabled (426 prompt / 71 completion tokens).
- A caller without the application identity rejected at the gateway with 401, visible in Application Insights.
- Web, API and gateway health checks and smoke tests passing after each release.

Ready to demonstrate on request: a scripted throttling drill (`scripts/pfa-throttle-drill.sh`) lowers the gateway rate limit to produce real 429s while users keep receiving compliant answers.

Technical detail: [README](../README.md) · Operations: [RUNBOOK](RUNBOOK.md) · Demo: [DEMO_SCRIPT](DEMO_SCRIPT.md)

"""Compliance-forced agents. These are never chosen by the intent router; only guardrails route here.

* PharmacovigilanceAgent — adverse-event intake. Creates an AE case reference, writes an
  ``ae.flagged`` audit event, and gives the rep the mandatory reporting steps and deadline.
* MedicalInformationAgent — unsolicited off-label requests. The rep cannot answer; we offer to
  file a Medical Information Request (MIR) so Medical Affairs responds (confirm before write).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.agents.base import AgentContext, AgentResult, PendingAction
from app.compliance.guardrails import load_policy, mentioned_products


class PharmacovigilanceAgent:
    name = "pharmacovigilance"
    description = "Adverse-event intake (forced route)"

    async def run(self, ctx: AgentContext) -> AgentResult:
        policy = load_policy()
        case_ref = f"AE-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        due = datetime.now(UTC) + timedelta(hours=policy.ae_reporting_window_hours)
        products = mentioned_products(ctx.message, policy)
        return AgentResult(
            answer=(
                f"⚠️ **This looks like a potential adverse event.** I've opened intake reference **{case_ref}** "
                f"and notified Pharmacovigilance. You are required to complete the AE report within "
                f"{policy.ae_reporting_window_hours} hours (by {due:%b %d %H:%M} UTC), even if you're unsure the "
                "event is product-related.\n\n"
                "1. Do not give the HCP medical advice about the event.\n"
                "2. Note the four minimum criteria: an identifiable reporter, an identifiable patient (no names needed), "
                "the suspect product, and the event.\n"
                "3. Complete the AE form in Veeva (Safety tab) or call PV at 1-800-555-0199.\n\n"
                "Patient identifiers in your message were redacted before processing."
            ),
            data={"ae_case_ref": case_ref, "due_utc": due.isoformat(), "products": products},
            audit_events=[("ae.flagged", {"ae_case_ref": case_ref, "products": products, "due_utc": due.isoformat()})],
        )


class MedicalInformationAgent:
    name = "medical_information"
    description = "Unsolicited off-label request handling (forced route)"

    async def run(self, ctx: AgentContext) -> AgentResult:
        policy = load_policy()
        products = mentioned_products(ctx.message, policy)
        names = ", ".join(policy.approved_products[p]["display_name"] for p in products) or "the product"
        populations = "; ".join(
            f"{policy.approved_products[p]['display_name']}: {policy.approved_products[p]['approved_population']}" for p in products
        )
        action = PendingAction(
            kind="medinfo.submit_mir",
            summary=f"Submit a Medical Information Request about {names} on behalf of the HCP",
            params={"products": products, "question": ctx.message},
            owner_oid=ctx.principal.oid,
            correlation_id=ctx.correlation_id,
        )
        return AgentResult(
            answer=(
                f"That question concerns a use of {names} outside its approved labeling, so I can't discuss it "
                f"and neither can you in a promotional setting. Approved population: {populations}.\n\n"
                f"If the HCP asked this **unsolicited**, I can file a Medical Information Request so Medical Affairs "
                f"responds directly. {policy.medical_information_contact}."
            ),
            pending_action=action,
        )

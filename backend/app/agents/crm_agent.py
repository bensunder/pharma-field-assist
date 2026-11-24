"""Veeva CRM agent: account lookups and call logging (write -> human confirmation)."""

from __future__ import annotations

import re

from app.agents.base import AgentContext, AgentResult, PendingAction
from app.compliance.guardrails import load_policy, mentioned_products
from app.core.errors import DependencyUnavailableError
from app.security.obo import CRM_SCOPE

_LOG_CALL = re.compile(r"\blog (?:a )?call\b", re.IGNORECASE)
_NOTES = re.compile(r"(?:notes?|:)\s*(.+)$", re.IGNORECASE | re.DOTALL)


class CrmAgent:
    name = "crm"
    description = "Looks up Veeva accounts in your territory and logs calls (confirm before write)"

    async def run(self, ctx: AgentContext) -> AgentResult:
        crm = ctx.services.crm
        try:
            token = await ctx.delegated(CRM_SCOPE)
            if _LOG_CALL.search(ctx.message):
                return await self._log_call(ctx, token)
            account = await ctx.call_tool(
                "crm.findAccount",
                "crm",
                lambda: crm.find_account(token, ctx.principal.territory, ctx.message),
                token=token,
                identity=crm.identity(token),
            )
            if account:
                last = account["last_call"] or "no call on record"
                return AgentResult(
                    answer=(
                        f"**{account['name']}**, {account['specialty']} at {account['organization']} "
                        f"(segment {account['segment']}, {account['territory']}). Products: "
                        f"{', '.join(account['products'])}. Last call: {last}."
                    ),
                    data={"accounts": [account]},
                )
            accounts = await ctx.call_tool(
                "crm.listAccounts",
                "crm",
                lambda: crm.list_accounts(token, ctx.principal.territory),
                token=token,
                identity=crm.identity(token),
            )
        except DependencyUnavailableError:
            return AgentResult(
                answer="Veeva CRM is temporarily unreachable. Nothing was written. Please retry in a few minutes.",
                degraded=True,
            )
        accounts = sorted(accounts, key=lambda a: a["segment"])
        lines = "\n".join(f"- {a['name']} ({a['specialty']}, seg {a['segment']}) — {a['organization']}" for a in accounts)
        return AgentResult(
            answer=f"You have {len(accounts)} accounts visible in {ctx.principal.territory}:\n{lines}",
            data={"accounts": accounts},
        )

    async def _log_call(self, ctx: AgentContext, token) -> AgentResult:
        crm = ctx.services.crm
        account = await ctx.call_tool(
            "crm.findAccount",
            "crm",
            lambda: crm.find_account(token, ctx.principal.territory, ctx.message),
            token=token,
            identity=crm.identity(token),
        )
        if not account:
            return AgentResult(answer="I couldn't find that HCP in your territory. Which account should I log the call against?")
        m = _NOTES.search(ctx.message.split(account["name"].split()[-1], 1)[-1])
        notes = (m.group(1).strip() if m else "").strip(" .")
        if len(notes) < 5:
            return AgentResult(answer=f"What should the call notes for {account['name']} say?")
        products = mentioned_products(notes, load_policy()) or account["products"]
        action = PendingAction(
            kind="crm.log_call",
            summary=f"Log call for {account['name']} — products: {', '.join(products)} — notes: “{notes}”",
            params={"account_id": account["account_id"], "notes": notes, "products": products},
            owner_oid=ctx.principal.oid,
            correlation_id=ctx.correlation_id,
        )
        return AgentResult(
            answer=f"Ready to submit to Veeva: **{action.summary}**. Notes were PII-screened. Confirm to submit.",
            pending_action=action,
        )

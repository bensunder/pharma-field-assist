"""Field analytics agent (Microsoft Fabric).

Natural language is parsed into a *whitelisted* query spec (metric, product, territory, window);
the model never writes SQL. Territory scope comes from the caller's identity, not from the prompt:
a rep asking for another territory is denied (AUTHZ-201) and the denial is audited.
"""

from __future__ import annotations

import re

from app.agents.base import AgentContext, AgentResult
from app.compliance.guardrails import load_policy, mentioned_products
from app.core.errors import AuthorizationError, DependencyUnavailableError

_TERRITORY = re.compile(r"\b([A-Z]{2}-[A-Z]{3}(?:-\d{2})?)\b", re.IGNORECASE)
_WEEKS = re.compile(r"\b(?:last|past)\s+(\d{1,2})\s+weeks?\b", re.IGNORECASE)


def parse_metric(text: str) -> str:
    t = text.lower()
    if "nrx" in t or "new prescription" in t or "new rx" in t:
        return "nrx"
    if "share" in t:
        return "market_share"
    return "trx"


def parse_weeks(text: str) -> int:
    m = _WEEKS.search(text)
    if m:
        return max(2, min(int(m.group(1)), 12))
    if "quarter" in text.lower() or "qtd" in text.lower():
        return 12
    return 8


def resolve_scope(requested: str | None, principal) -> str:
    """Return the territory prefix the caller may query. Raises if the request exceeds scope."""
    own = principal.territory
    if requested is None:
        return own
    requested = requested.upper()
    if own == "US" or requested.startswith(own):
        return requested
    raise AuthorizationError(
        f"Territory {requested} is outside your data scope ({own})",
        details={"rule_id": "AUTHZ-201", "requested": requested, "scope": own},
    )


def _fmt(metric: str, v: float) -> str:
    return f"{v:.1%}" if metric == "market_share" else f"{v:,.0f}"


class AnalyticsAgent:
    name = "analytics"
    description = "TRx / NRx / market-share trends from Fabric, scoped to the caller's territory"

    async def run(self, ctx: AgentContext) -> AgentResult:
        fabric = ctx.services.fabric
        products = mentioned_products(ctx.message, load_policy()) or ["cardivex"]
        product = products[0]
        metric = parse_metric(ctx.message)
        weeks = parse_weeks(ctx.message)
        m = _TERRITORY.search(ctx.message)
        scope = resolve_scope(m.group(1) if m else None, ctx.principal)

        try:
            series = await ctx.call_tool(
                "fabric.sql.weekly_rx_by_territory",
                "fabric",
                lambda: fabric.weekly_series(metric=metric, product=product, territory_prefix=scope, weeks=weeks),
                identity=fabric.identity(),
            )
        except DependencyUnavailableError:
            return AgentResult(
                answer="Fabric analytics is temporarily unavailable. Try again shortly. Your last synced "
                "dashboard is still available in the Field Insights Power BI app.",
                degraded=True,
            )
        if not series:
            return AgentResult(answer=f"No {metric.upper()} data for {product} in {scope} over the last {weeks} weeks.")

        aggregate = metric != "market_share"
        weeks_axis = sorted({p["week_start"] for s in series.values() for p in s})
        by_week = {w: [p["value"] for s in series.values() for p in s if p["week_start"] == w] for w in weeks_axis}
        totals = [(sum(v) if aggregate else sum(v) / len(v)) for v in (by_week[w] for w in weeks_axis)]
        latest, prior, first = totals[-1], totals[-2] if len(totals) > 1 else totals[-1], totals[0]
        wow = (latest - prior) / prior if prior else 0.0
        window = (latest - first) / first if first else 0.0
        label = {"trx": "TRx", "nrx": "NRx", "market_share": "market share"}[metric]
        product_name = load_policy().approved_products[product]["display_name"].split(" (")[0]

        summary = (
            f"**{product_name} {label} — {scope}, last {weeks} weeks.** Latest week "
            f"({weeks_axis[-1]}): {_fmt(metric, latest)} ({wow:+.1%} WoW). Over the window: {window:+.1%}."
        )
        if len(series) > 1:
            ranked = sorted(series.items(), key=lambda kv: kv[1][-1]["value"], reverse=True)
            summary += f" Top territory this week: {ranked[0][0]} ({_fmt(metric, ranked[0][1][-1]['value'])})."

        return AgentResult(
            answer=summary + f"\n\n*Source: {fabric.dataset_name}, refreshed {fabric.refreshed_at}.*",
            data={
                "chart": {
                    "metric": metric,
                    "product": product,
                    "weeks": weeks_axis,
                    "series": {t: [p["value"] for p in s] for t, s in series.items()},
                    "total": [round(v, 4) for v in totals],
                }
            },
        )

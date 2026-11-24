"""Product Q&A agent — grounded answers from MLR-approved Markdown content only.

Pipeline: detect product -> BM25 retrieve (product-filtered, score floor) -> generate
(LLM constrained to sources, or extractive in offline mode) -> return with citations.

An answer cache keyed on (normalised question, product, KB version) avoids repeat LLM calls for the
same field question across reps; the KB version in the key means a content release invalidates it
automatically.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentContext, AgentResult
from app.compliance.guardrails import load_policy, mentioned_products
from app.core.errors import DependencyUnavailableError
from app.knowledge.index import ScoredChunk, tokenize
from app.llm.provider import complete_json, describe_llm_result

_SYSTEM = (
    "You are a compliant pharmaceutical field assistant. Answer ONLY using the numbered sources. "
    "Cite every factual sentence with [n]. Never make comparative or superlative claims, never discuss "
    "unapproved uses, never give patient-specific medical advice. Keep answers under 120 words. "
    'Respond with a JSON object only: {"grounded": true, "answer": "<answer with [n] citations>"}. '
    'If the sources do not answer the question respond {"grounded": false, "answer": ""}.'
)

QA_MAX_TOKENS = 300  # ~120-word answer + JSON wrapper; also clamped by PFA_LLM_MAX_OUTPUT_TOKENS


class QAOutput(BaseModel):
    """Schema the product-QA model reply must match; anything else -> extractive fallback."""

    model_config = ConfigDict(extra="ignore")
    grounded: bool
    answer: str = Field(default="", max_length=1500)


# Field vocabulary -> label vocabulary. Curated with Medical; versioned with the KB.
_QUERY_SYNONYMS = {
    "effective": "efficacy clinical",
    "work": "efficacy",
    "side effect": "adverse reactions",
    "cost": "copay savings",
    "insurance": "coverage formulary",
    "pa ": "prior authorization ",
    "kidney": "renal",
    "liver": "hepatic liver",
}


def expand_query(question: str) -> str:
    q = question.lower()
    extra = [v for k, v in _QUERY_SYNONYMS.items() if k in q]
    return f"{question} {' '.join(extra)}" if extra else question


def _product_terms(policy) -> set[str]:
    terms: set[str] = {"hfa"}
    for key, meta in policy.approved_products.items():
        terms.add(key)
        terms.update(re.findall(r"[a-z]+", meta["display_name"].lower()))
    return terms


class AnswerCache:
    def __init__(self, max_items: int = 512) -> None:
        self._items: OrderedDict[tuple, AgentResult] = OrderedDict()
        self._max = max_items
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(question: str, product: str | None, kb_version: str) -> tuple:
        return (" ".join(sorted(set(tokenize(question)))), product, kb_version)

    def get(self, key: tuple) -> AgentResult | None:
        item = self._items.get(key)
        if item is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item.model_copy(deep=True)  # callers mutate results; never hand out the cached instance

    def put(self, key: tuple, value: AgentResult) -> None:
        # snapshot before the orchestrator appends fair balance / tool calls to the live result
        self._items[key] = value.model_copy(deep=True)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


def _extractive_answer(question: str, hits: list[ScoredChunk], max_sentences: int = 3) -> tuple[str, list[int]]:
    q = set(tokenize(question))
    candidates: list[tuple[float, int, int, str]] = []
    top_score = hits[0].score
    for ci, hit in enumerate(hits):
        # relative cutoff: weak secondary chunks only add noise (and unwanted product content)
        if hit.score < 0.6 * top_score:
            continue
        for si, sentence in enumerate(re.split(r"(?<=[.!?])\s+", hit.chunk.text)):
            overlap = len(q & set(tokenize(sentence)))
            if overlap:
                # weight by chunk rank so the best chunk dominates; keep original order as tiebreak
                candidates.append((overlap * hit.score, ci, si, sentence.strip()))
    if not candidates:
        # fall back to the lead of the best chunk
        lead = re.split(r"(?<=[.!?])\s+", hits[0].chunk.text)[:2]
        return " ".join(s + " [1]" for s in lead), [0]
    best = max(c[0] for c in candidates)
    candidates = [c for c in candidates if c[0] >= 0.5 * best]
    picked = sorted(candidates, key=lambda c: c[0], reverse=True)[:max_sentences]
    picked.sort(key=lambda c: (c[1], c[2]))
    used = sorted({c[1] for c in picked})
    ref = {ci: n + 1 for n, ci in enumerate(used)}
    return " ".join(f"{c[3]} [{ref[c[1]]}]" for c in picked), used


class ProductQAAgent:
    name = "product_qa"
    description = "Answers label, access and field-policy questions from MLR-approved content"

    def __init__(self) -> None:
        self.cache = AnswerCache()

    async def run(self, ctx: AgentContext) -> AgentResult:
        services = ctx.services
        policy = load_policy()
        products = mentioned_products(ctx.message, policy)
        product = products[0] if len(products) == 1 else None
        kb = services.knowledge

        cache_key = AnswerCache.key(ctx.message, product, kb.version)
        if cached := self.cache.get(cache_key):
            cached.data = {**(cached.data or {}), "cache": "hit"}
            return cached

        async def _retrieve() -> list[ScoredChunk]:
            return kb.search(
                expand_query(ctx.message),
                top_k=services.settings.retrieval_top_k,
                product=product,
                min_score=services.settings.retrieval_min_score,
                non_topical=_product_terms(policy),
            )

        hits = await ctx.call_tool("knowledge.search", "knowledge", _retrieve, identity="in-process (MLR-approved index)")
        if not hits:
            return AgentResult(answer="", citations=[], is_product_content=True, data={"retrieval": []})

        retrieval = [{"citation": h.chunk.citation, "score": h.score, "chunk_id": h.chunk.chunk_id} for h in hits]
        answer, used = await self._generate(ctx, hits)
        if answer is None:
            return AgentResult(answer="", citations=[], is_product_content=True, data={"retrieval": retrieval})

        citations = [hits[i].chunk.citation for i in used]
        result = AgentResult(
            answer=answer,
            citations=citations,
            is_product_content=bool(products) or any(hits[i].chunk.product for i in used),
            tool_calls=[],
            data={"retrieval": retrieval, "mode": "llm" if services.llm.enabled else "extractive", "cache": "miss"},
        )
        self.cache.put(cache_key, result)
        return result

    async def _generate(self, ctx: AgentContext, hits: list[ScoredChunk]) -> tuple[str | None, list[int]]:
        llm = ctx.services.llm
        if llm.enabled:
            sources = "\n".join(f"[{i + 1}] ({h.chunk.citation}) {h.chunk.text}" for i, h in enumerate(hits))
            try:
                result = await ctx.call_tool(
                    "llm.complete",
                    "azure-openai",
                    lambda: complete_json(
                        llm, _SYSTEM, f"Sources:\n{sources}\n\nQuestion: {ctx.message}", schema=QAOutput, max_tokens=QA_MAX_TOKENS
                    ),
                    identity="app:managed-identity → "
                    + ("APIM gateway → Azure OpenAI" if getattr(llm, "via_gateway", False) else "Azure OpenAI"),
                    describe=describe_llm_result,
                )
            except DependencyUnavailableError:
                # Model down -> extractive answer from the same approved sources (degraded, still grounded)
                return _extractive_answer(ctx.message, hits)
            if not result.data.grounded or not result.data.answer.strip():
                return None, []
            text = result.data.answer
            cited = {int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(hits)}
            if not cited:
                return None, []  # uncited model output is treated as ungrounded
            # keep source numbering stable so [n] in the text matches citations[n-1]
            return text.strip(), list(range(max(cited)))
        return _extractive_answer(ctx.message, hits)

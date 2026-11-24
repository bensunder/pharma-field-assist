"""Markdown knowledge retrieval pipeline.

Ingest:  *.md with YAML-ish front matter (title, product, mlr_id, approved_until, audience)
Govern:  documents past ``approved_until`` or missing an MLR id are EXCLUDED at index time —
         a rep can never be served expired promotional content, even if the file is still in git.
Chunk:   split on H2/H3 headings; each chunk keeps doc + section for citation.
Retrieve: BM25 (Okapi) over chunk text with title/section boost. Deterministic and explainable,
         which matters in MLR review; the ``Retriever`` protocol allows swapping in Azure AI Search
         hybrid (BM25 + vector + semantic ranker) without touching agents.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by can do does for from how i in is it my of on or should the to what when "
    "which who why will with you your me we about tell".split()
)


_SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ages", "age", "ated", "ed", "es", "s", "e")


def stem(token: str) -> str:
    """Tiny suffix stripper: dose/dosed/dosing/dosage -> dos, indicated/indication -> indic."""
    if token.isdigit() or len(token) <= 3:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    product: str | None
    mlr_id: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.title} § {self.section} [{self.mlr_id}]"


@dataclass
class DocMeta:
    doc_id: str
    title: str
    product: str | None
    mlr_id: str | None
    approved_until: date | None
    indexed: bool
    exclusion_reason: str | None = None


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float


@dataclass
class KnowledgeIndex:
    chunks: list[Chunk] = field(default_factory=list)
    documents: list[DocMeta] = field(default_factory=list)
    version: str = ""
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avgdl: float = 0.0

    k1: float = 1.4
    b: float = 0.75

    # ---------------------------------------------------------------- build
    @classmethod
    def from_directory(cls, directory: Path, *, today: date | None = None) -> KnowledgeIndex:
        today = today or date.today()
        idx = cls()
        hasher = hashlib.sha256()
        for path in sorted(directory.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            hasher.update(path.name.encode() + raw.encode())
            meta, body = _parse_front_matter(raw)
            doc = DocMeta(
                doc_id=path.stem,
                title=meta.get("title", path.stem),
                product=meta.get("product") or None,
                mlr_id=meta.get("mlr_id") or None,
                approved_until=_parse_date(meta.get("approved_until")),
                indexed=True,
            )
            if not doc.mlr_id:
                doc.indexed, doc.exclusion_reason = False, "missing MLR approval id"
            elif doc.approved_until and doc.approved_until < today:
                doc.indexed, doc.exclusion_reason = False, f"MLR approval expired {doc.approved_until}"
            idx.documents.append(doc)
            if doc.indexed:
                idx.chunks.extend(_chunk(doc, body))
        idx.version = hasher.hexdigest()[:12]
        idx._build_stats()
        return idx

    def _build_stats(self) -> None:
        self._tf = []
        self._df = Counter()
        for c in self.chunks:
            # title + section are repeated once to act as a field boost
            tf = Counter(tokenize(f"{c.title} {c.section} {c.section} {c.text}"))
            self._tf.append(tf)
            self._df.update(tf.keys())
        self._avgdl = (sum(sum(t.values()) for t in self._tf) / len(self._tf)) if self._tf else 0.0

    # ---------------------------------------------------------------- query
    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        product: str | None = None,
        min_score: float = 0.0,
        non_topical: set[str] | None = None,
    ) -> list[ScoredChunk]:
        """BM25 search. ``non_topical`` terms (e.g. product names) still score, but a chunk must
        match at least one *other* query term — "Cardivex launch messaging" must not return the
        dosing section just because it says "Cardivex"."""
        q = tokenize(query)
        if not q or not self.chunks:
            return []
        ignore = {stem(t) for t in (non_topical or set())}
        topical = set(q) - ignore
        n = len(self.chunks)
        results: list[ScoredChunk] = []
        for i, chunk in enumerate(self.chunks):
            if product and chunk.product not in (product, None):
                continue
            tf = self._tf[i]
            if topical and not any(t in tf for t in topical):
                continue
            dl = sum(tf.values())
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                df = self._df[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            if score >= min_score and score > 0:
                results.append(ScoredChunk(chunk, round(score, 3)))
        results.sort(key=lambda s: s.score, reverse=True)
        return results[:top_k]

    def isi_for(self, product: str) -> str | None:
        for c in self.chunks:
            if c.product == product and c.doc_id.endswith("-isi"):
                return c.text
        return None


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    _, fm, body = raw.split("---", 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, body


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_HEADING = re.compile(r"^(#{2,3})\s+(.*)$", re.MULTILINE)


def _chunk(doc: DocMeta, body: str) -> list[Chunk]:
    parts: list[tuple[str, str]] = []
    matches = list(_HEADING.finditer(body))
    if not matches:
        parts.append(("Overview", body.strip()))
    else:
        preamble = body[: matches[0].start()].strip()
        preamble = re.sub(r"^#\s+.*$", "", preamble, flags=re.MULTILINE).strip()
        if preamble:
            parts.append(("Overview", preamble))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            text = body[m.end() : end].strip()
            if text:
                parts.append((m.group(2).strip(), text))
    out = []
    for n, (section, text) in enumerate(parts):
        out.append(
            Chunk(
                chunk_id=f"{doc.doc_id}#{n}",
                doc_id=doc.doc_id,
                title=doc.title,
                section=section,
                product=doc.product,
                mlr_id=doc.mlr_id or "",
                text=re.sub(r"\s+", " ", text),
            )
        )
    return out

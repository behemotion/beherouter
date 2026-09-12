"""Embeddings-free BM25 tool index (rank-bm25).

Hard constraint from AGENTS.md: tool search must be lexical, never embeddings.
At ~100 tools per surface BM25 is accurate enough, and it stays testable and
dependency-light.
"""

import re

from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz

_TOKEN = re.compile(r"[a-z0-9]+")

# A query token shorter than this never prefix-matches: "a" would otherwise
# select most of the corpus and destroy precision.
_MIN_PREFIX = 3
# rapidfuzz ratio 0-100. 80 accepts one or two typos in a short word without
# pulling in unrelated tokens.
_FUZZ_THRESHOLD = 80


def _tok(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class ToolIndex:
    """Lazily-built BM25 index over `<name> <summary> <keywords>`."""

    def __init__(self) -> None:
        self._names: list[str] = []
        self._corpus: list[list[str]] = []
        self._token_sets: list[set[str]] = []
        self._bm25: BM25Okapi | None = None

    def add(self, name: str, summary: str, keywords: list[str]) -> None:
        tokens = _tok(f"{name} {summary} {' '.join(keywords)}")
        self._names.append(name)
        self._corpus.append(tokens)
        self._token_sets.append(set(tokens))
        self._bm25 = None  # invalidate — rebuilt on next search

    def _ensure(self) -> None:
        if self._bm25 is None and self._corpus:
            self._bm25 = BM25Okapi(self._corpus)

    def search(self, query: str, limit: int = 10) -> list[str]:
        """Return up to `limit` tool names, ranked by BM25.

        Membership is decided in TIERS, each firing only when the previous one
        under-fills `limit`: exact token overlap, then prefix, then edit
        distance. BM25 still does the RANKING within a tier — it does not decide
        membership, because rank-bm25's Okapi IDF is negative for a term present
        in most of the corpus, so filtering on score sign would drop genuine
        matches (a term like "repo" in ~50 of 100 tools would return nothing).

        Tier order is also the sort key, so an exact hit always outranks a fuzzy
        one no matter what BM25 thinks of it.
        """
        self._ensure()
        if self._bm25 is None:
            return []
        q = set(_tok(query))
        if not q:
            return []

        tier: dict[int, int] = {}
        for i, toks in enumerate(self._token_sets):
            if q & toks:
                tier[i] = 0
        if len(tier) < limit:
            for i, toks in enumerate(self._token_sets):
                if i in tier:
                    continue
                if any(
                    len(qt) >= _MIN_PREFIX
                    and len(t) >= _MIN_PREFIX
                    and (t.startswith(qt) or qt.startswith(t))
                    for qt in q
                    for t in toks
                ):
                    tier[i] = 1
        if len(tier) < limit:
            for i, toks in enumerate(self._token_sets):
                if i in tier:
                    continue
                if any(
                    len(qt) >= _MIN_PREFIX and fuzz.ratio(qt, t) >= _FUZZ_THRESHOLD
                    for qt in q
                    for t in toks
                ):
                    tier[i] = 2
        if not tier:
            return []

        scores = self._bm25.get_scores(_tok(query))
        ordered = sorted(tier, key=lambda i: (tier[i], -scores[i], i))
        return [self._names[i] for i in ordered[:limit]]

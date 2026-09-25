"""Embeddings-free lexical tool index (rank-bm25 + rapidfuzz).

Hard constraint from AGENTS.md: tool search must be lexical, never embeddings.
At ~30-100 tools per surface a field-weighted BM25 with light normalisation is
accurate enough, and it stays testable and dependency-light.

The design is recorded in docs/superpowers/specs/2026-09-25-tool-search-quality-design.md;
every constant below is tuned against tests/test_search_eval.py, not by eye.
"""

import re
from functools import lru_cache

from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz, process

_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

# Words an agent writes in a natural-language query that carry no signal about
# WHICH tool. Without this list, "to"/"of"/"a" appear in nearly every
# description and used to decide membership on their own. Kept deliberately
# short: a word that names an action ("list", "use", "do") is NOT a stopword.
STOPWORDS = frozenset(
    "a an and are as at be by for from how i in into is it its me my of on or "
    "please so that the their them then there these this to we what when which "
    "who with you your".split()
)

# Plural folding is rule-based on purpose: no stemmer library, nothing
# downloaded at runtime (the same reason costing.py estimates tokens). The
# rules need only be CONSISTENT -- index and query go through the same function.
MIN_PLURAL_LEN = 5
_KEEP_S_ENDINGS = ("ss", "us", "is", "as")

# Field weights, applied by repetition in the one BM25 document per tool.
NAME_WEIGHT = 3
ALIAS_WEIGHT = 2
LEAD_WEIGHT = 2

# Match weights: how much a query token earns for each kind of match.
EXACT_WEIGHT = 1.0
PREFIX_WEIGHT = 0.6
FUZZY_WEIGHT = 0.4
MIN_PREFIX = 4  # "a" or "rep" would otherwise prefix-match half the corpus
MIN_FUZZY = 5  # fuzzy ratio on short words is noise
FUZZ_THRESHOLD = 85  # rapidfuzz ratio 0-100

# rank-bm25's Okapi IDF is 0 or negative for a term in half or more of the
# corpus (and for every term in a one-document corpus), so a genuine match can
# score <= 0. Every matched term earns at least this, so a match is never lost.
MATCH_FLOOR = 0.1

# A hit scoring below this fraction of the best one is noise, not a result.
RELATIVE_CUTOFF = 0.35
DEFAULT_LIMIT = 5

SUGGEST_CUTOFF = 60  # rapidfuzz ratio for "did you mean"


def singular(token: str) -> str:
    if (
        len(token) < MIN_PLURAL_LEN
        or not token.endswith("s")
        or token.endswith(_KEEP_S_ENDINGS)
    ):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("sses") or token.endswith(("xes", "zes", "ches", "shes")):
        return token[:-2]
    return token[:-1]


def normalize(text: str) -> list[str]:
    """The ONE tokenizer, for index and query alike."""
    text = _CAMEL.sub(r"\1 \2", text).lower()
    return [singular(t) for t in _WORD.findall(text) if t not in STOPWORDS]


def split_lead(text: str) -> tuple[str, str]:
    """(first sentence, rest). The first sentence stops at the first line break."""
    text = text.strip()
    newline = text.find("\n")
    first_line = text if newline < 0 else text[:newline]
    m = _SENTENCE_END.search(first_line)
    cut = m.end() if m else len(first_line)
    return text[:cut].strip(), text[cut:].strip()


def _name_forms(name: str) -> list[str]:
    """`workitem_comment` -> workitem, comment, workitemcomment."""
    tokens = normalize(name)
    return tokens + (["".join(tokens)] if len(tokens) > 1 else [])


class ToolIndex:
    """Lazily-built, field-weighted BM25 index over one document per tool."""

    def __init__(self) -> None:
        self._names: list[str] = []
        self._name_keys: list[str] = []
        self._corpus: list[list[str]] = []
        self._token_sets: list[set[str]] = []
        self._bm25: BM25Okapi | None = None
        self._vocab: set[str] = set()

    def add(
        self,
        name: str,
        summary: str,
        keywords: list[str],
        aliases: tuple[str, ...] | list[str] = (),
    ) -> None:
        lead, rest = split_lead(summary)
        doc = (
            _name_forms(name) * NAME_WEIGHT
            + [t for a in aliases for t in normalize(a)] * ALIAS_WEIGHT
            + normalize(lead) * LEAD_WEIGHT
            + normalize(rest)
            + [t for k in keywords for t in normalize(k)]
        )
        self._names.append(name)
        self._name_keys.append("".join(normalize(name)))
        self._corpus.append(doc)
        self._token_sets.append(set(doc))
        self._bm25 = None  # invalidate — rebuilt on next search

    def _ensure(self) -> None:
        if self._bm25 is None and self._corpus:
            self._bm25 = BM25Okapi(self._corpus)
            self._vocab = set().union(*self._token_sets)

    def _expand(self, qt: str) -> list[tuple[str, float]]:
        """Index terms a query token matches, with the weight each earns."""
        out = [(qt, EXACT_WEIGHT)] if qt in self._vocab else []
        for v in self._vocab:
            if v == qt:
                continue
            if (
                len(qt) >= MIN_PREFIX
                and len(v) >= MIN_PREFIX
                and (v.startswith(qt) or qt.startswith(v))
            ):
                out.append((v, PREFIX_WEIGHT))
            elif (
                len(qt) >= MIN_FUZZY
                and len(v) >= MIN_FUZZY
                and fuzz.ratio(qt, v) >= FUZZ_THRESHOLD
            ):
                out.append((v, FUZZY_WEIGHT))
        return out

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        """Up to `limit` tool names, best first.

        Each query token contributes its BEST match per tool (exact, prefix or
        fuzzy), so one token cannot score twice through two spellings. A query
        equal to a tool's name (after normalisation, with or without the
        separators) puts that tool first. Hits under RELATIVE_CUTOFF of the
        best finite score are dropped.
        """
        self._ensure()
        if self._bm25 is None:
            return []
        q = normalize(query)
        if not q:
            return []
        n = len(self._names)
        scores = [0.0] * n
        for qt in dict.fromkeys(q):
            best = [0.0] * n
            # An expansion earns the IDF of the term it MATCHED, and a rare
            # misspelling ("seaarch") has a far higher IDF than the common word
            # the agent typed -- so uncapped, a typo'd tool outranked every exact
            # hit. When the query token matches exactly somewhere, an expansion
            # earns at most its weight times that best exact score.
            exact_best = 0.0
            for term, weight in self._expand(qt):
                term_scores = self._bm25.get_scores([term])
                for i, toks in enumerate(self._token_sets):
                    if term in toks:
                        raw = max(term_scores[i], 0.0) + MATCH_FLOOR
                        if term == qt:
                            exact_best = max(exact_best, raw)
                        elif exact_best:
                            raw = min(raw, exact_best)
                        best[i] = max(best[i], weight * raw)
            for i in range(n):
                scores[i] += best[i]

        key = "".join(q)
        exact = [i for i, k in enumerate(self._name_keys) if k and k == key]
        rest = [i for i in range(n) if scores[i] > 0 and i not in exact]
        if not exact and not rest:
            return []
        top = max((scores[i] for i in rest), default=0.0)
        rest = sorted(
            (i for i in rest if scores[i] >= RELATIVE_CUTOFF * top),
            key=lambda i: (-scores[i], i),
        )
        return [self._names[i] for i in (exact + rest)[:limit]]


BRIEF_MAX = 160


@lru_cache(maxsize=4096)
def brief(summary: str) -> str:
    """The first sentence of a description, capped — what search_tools returns.

    Cached by summary text, so it is computed once per distinct description
    rather than on every call (a catalogue refresh with unchanged text hits).
    """
    lead, _ = split_lead(summary)
    return lead if len(lead) <= BRIEF_MAX else lead[: BRIEF_MAX - 1].rstrip() + "…"


def suggest(name: str, candidates: list[str], index: ToolIndex | None = None) -> list[str]:
    """Up to three 'did you mean' names: close spellings, else search hits."""
    close = [
        m
        for m, _score, _i in process.extract(
            name, candidates, scorer=fuzz.ratio, limit=3, score_cutoff=SUGGEST_CUTOFF
        )
    ]
    if close or index is None:
        return close
    return index.search(name, limit=3)

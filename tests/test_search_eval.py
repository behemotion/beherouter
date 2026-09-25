"""Search quality gates against real, recorded backend catalogues.

The catalogue is a snapshot, not a mock: re-record it with
scripts/record_catalogue.py after upgrading the backend, then re-run this file.
A failure prints the whole table, because "top-3 fell to 87%" says nothing
about which query to look at.
"""

import json
import tomllib
from pathlib import Path

from beherouter.costing import estimate_tokens
from beherouter.indexing import build_index, search_hits
from beherouter.plugins.plane import PINNED, SEARCH_ALIASES
from beherouter.search import DEFAULT_LIMIT

QUERIES = Path(__file__).parent / "search_eval" / "queries"

# Set just under what the scorer measured on 2026-09-25 (top-3 39/40, top-1
# 32/40, 2.9 hits per query, 126 tokens per search): tight enough to catch a
# regression, not so loose that one hides. The design's floors were 0.90 / 0.75
# / 4.0; never loosen past them to make a change pass.
MIN_TOP3 = 0.95
MIN_TOP1 = 0.80
MAX_MEAN_HITS = 3.0
MAX_NEGATIVE_HITS = 1
MAX_PAYLOAD_TOKENS = 400


def test_plane_catalogue_fixture_loads(catalogue_descriptors):
    ds = catalogue_descriptors("plane-0.3.2")
    assert len(ds) == 30
    assert {"workitem", "cycle", "state", "member"} <= {d.name for d in ds}


def test_plane_search_quality(catalogue_descriptors):
    ds = catalogue_descriptors("plane-0.3.2", pinned=PINNED)
    by_name = {d.name: d for d in ds}
    index = build_index(ds, SEARCH_ALIASES)
    published = set(PINNED)
    spec = tomllib.loads((QUERIES / "plane.toml").read_text())

    rows, top1, top3, total_hits, max_tokens = [], 0, 0, 0, 0
    for case in spec["query"]:
        hits = search_hits(index, by_name, case["q"], DEFAULT_LIMIT, published)
        names = [h["name"] for h in hits]
        rank = names.index(case["expect"]) + 1 if case["expect"] in names else None
        top1 += rank == 1
        top3 += bool(rank and rank <= 3)
        total_hits += len(names)
        max_tokens = max(max_tokens, estimate_tokens(json.dumps(hits)))
        rows.append(f"{case['q']:40} want={case['expect']:22} rank={rank} got={names}")
    neg_bad = []
    for case in spec["negative"]:
        names = [h["name"] for h in search_hits(index, by_name, case["q"], DEFAULT_LIMIT, published)]
        rows.append(f"{case['q']:40} want=(nothing){'':13} got={names}")
        if len(names) > MAX_NEGATIVE_HITS:
            neg_bad.append(case["q"])

    n = len(spec["query"])
    summary = (
        f"top1 {top1}/{n} ({top1 / n:.0%}), top3 {top3}/{n} ({top3 / n:.0%}), "
        f"mean hits {total_hits / n:.1f}, max payload ~{max_tokens} tokens, "
        f"noisy negatives {neg_bad}"
    )
    table = "\n".join(rows) + "\n" + summary
    assert top3 / n >= MIN_TOP3, table
    assert top1 / n >= MIN_TOP1, table
    assert total_hits / n <= MAX_MEAN_HITS, table
    assert not neg_bad, table
    assert max_tokens <= MAX_PAYLOAD_TOKENS, table

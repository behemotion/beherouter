"""Search quality gates against real, recorded backend catalogues.

Each catalogue is a snapshot, not a mock: re-record it after upgrading the
backend (scripts/record_catalogue.py for a uvx stdio server; the other recipes,
and every catalogue's provenance, are in tests/search_eval/catalogues/README.md),
then re-run this file. One eval set per plugin, in SUITES below.
A failure prints the whole table, because "top-3 fell to 87%" says nothing
about which query to look at.
"""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from beherouter.costing import estimate_tokens
from beherouter.indexing import build_index, search_hits
from beherouter.plugins import office_mcp, plane, sonarqube
from beherouter.search import DEFAULT_LIMIT

QUERIES = Path(__file__).parent / "search_eval" / "queries"


@dataclass(frozen=True)
class Gate:
    """Floors for one eval set. Set just under what the scorer measured, so a
    regression trips them and one cannot hide. The design's floors were
    0.90 / 0.75 / 4.0; never loosen past them to make a change pass."""

    min_top3: float = 0.95
    min_top1: float = 0.80
    max_mean_hits: float = 3.0
    max_negative_hits: int = 1
    max_payload_tokens: int = 400


# (eval set, catalogue fixture, the plugin spec whose pins and aliases it gates)
SUITES = {
    # Measured 2026-09-25: top-3 39/40, top-1 32/40, 2.9 hits/query, 126 tokens.
    "plane": ("plane-0.3.2", plane.SPEC, Gate()),
    # Measured 2026-10-07: top-3 33/33, top-1 28/33, 2.5 hits/query, 170 tokens
    # (top-3 23/33, top-1 20/33 before the plugin's search_aliases existed).
    "sonarqube": ("sonarqube-1.27.0.4335", sonarqube.SPEC, Gate()),
    # Measured 2026-10-07: top-3 18/18, top-1 18/18, 1.2 hits/query, 40 tokens
    # (top-3 9/18, top-1 8/18 without aliases: no capability word reached
    # `discover`). Four tools, so one miss is 6% -- floors allow exactly one.
    "office-mcp": (
        "office-mcp-0.1.0",
        office_mcp.SPEC,
        Gate(min_top3=0.94, min_top1=0.90, max_mean_hits=2.0, max_payload_tokens=150),
    ),
}


def test_plane_catalogue_fixture_loads(catalogue_descriptors):
    ds = catalogue_descriptors("plane-0.3.2")
    assert len(ds) == 30
    assert {"workitem", "cycle", "state", "member"} <= {d.name for d in ds}


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_catalogue_serves_the_plugin(suite, catalogue_descriptors):
    """Every pin and every alias target names a tool the recorded backend
    serves -- otherwise the eval would be scoring a surface that does not exist."""
    fixture, spec, _ = SUITES[suite]
    served = {d.name for d in catalogue_descriptors(fixture)}
    assert set(spec.pinned) <= served
    assert set(spec.search_aliases) <= served


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_search_quality(suite, catalogue_descriptors):
    fixture, spec, gate = SUITES[suite]
    pinned = tuple(spec.pinned)
    ds = catalogue_descriptors(fixture, pinned=pinned)
    by_name = {d.name: d for d in ds}
    index = build_index(ds, {k: tuple(v) for k, v in spec.search_aliases.items()})
    published = set(pinned)
    cases = tomllib.loads((QUERIES / f"{suite}.toml").read_text())

    rows, top1, top3, total_hits, max_tokens = [], 0, 0, 0, 0
    for case in cases["query"]:
        assert case["expect"] in by_name, f"{case['expect']} is not in {fixture}"
        hits = search_hits(index, by_name, case["q"], DEFAULT_LIMIT, published)
        names = [h["name"] for h in hits]
        rank = names.index(case["expect"]) + 1 if case["expect"] in names else None
        top1 += rank == 1
        top3 += bool(rank and rank <= 3)
        total_hits += len(names)
        max_tokens = max(max_tokens, estimate_tokens(json.dumps(hits)))
        rows.append(f"{case['q']:40} want={case['expect']:22} rank={rank} got={names}")
    neg_bad = []
    for case in cases["negative"]:
        hits = search_hits(index, by_name, case["q"], DEFAULT_LIMIT, published)
        names = [h["name"] for h in hits]
        rows.append(f"{case['q']:40} want=(nothing){'':13} got={names}")
        if len(names) > gate.max_negative_hits:
            neg_bad.append(case["q"])

    n = len(cases["query"])
    summary = (
        f"{suite}: top1 {top1}/{n} ({top1 / n:.0%}), top3 {top3}/{n} ({top3 / n:.0%}), "
        f"mean hits {total_hits / n:.1f}, max payload ~{max_tokens} tokens, "
        f"noisy negatives {neg_bad}"
    )
    table = "\n".join(rows) + "\n" + summary
    print(table)
    assert top3 / n >= gate.min_top3, table
    assert top1 / n >= gate.min_top1, table
    assert total_hits / n <= gate.max_mean_hits, table
    assert not neg_bad, table
    assert max_tokens <= gate.max_payload_tokens, table

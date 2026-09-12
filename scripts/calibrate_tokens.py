"""Dev-only: calibrate CHARS_PER_TOKEN against real MCP tool definitions.

NOT shipped and NOT a dependency. tiktoken downloads its BPE vocab over the
network on first use, which is exactly why the runtime estimator is
dependency-free -- the gateway container has no egress. Run this on a
workstation:

    uv run --with tiktoken python scripts/calibrate_tokens.py

It writes tests/fixtures/token_calibration.json, which pins the divisor so a
future change to what beherouter publishes shows up as a test failure rather
than as a silently wrong cost.

Two surfaces are sampled, deliberately: `faketool` (a CLI backend whose
published tools are mostly beherouter's OWN meta-tools -- long English-prose
descriptions) and `gcal` (six real calendar tools with real, schema-dense JSON
Schema bodies, built from `plugins/calendar/tools.py` with no client, no
credentials and no network -- pure data). A sample of only the former measures
mostly English prose; the calendar tools are the punctuation-dense shape the
divisor is actually meant to describe. Mixing both is what makes the combined
figure representative of what beherouter actually publishes across surfaces.
"""

import asyncio
import json
from pathlib import Path

import tiktoken
from fastmcp import FastMCP

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.models import Backend
from beherouter.plugins.calendar.tools import VERBS, descriptors_for
from beherouter.surface import build_surface

FIXTURE = Path(__file__).parent.parent / "tests/fixtures/token_calibration.json"

# The four meta-tools (`search_tools`, `describe_tool`, `run_tool`,
# `context_cost`) are registered with the SAME name, description and schema on
# every surface (see `surface.build_surface`) -- so sampling N surfaces sees
# each of them N times over. A real deployment publishes them once per
# mounted surface too, but that is not what this script is estimating: it is
# calibrating a single characters-per-token DIVISOR meant to describe
# beherouter's typical published-tool text, and meta-tool prose is
# chars-per-token-heavier than dense JSON-Schema tool bodies. Counting the
# same prose once per sampled surface skews the aggregate toward it, which
# inflates the divisor -- and a larger divisor makes the estimator
# UNDER-report tokens, exactly the direction `costing.py` says must never
# happen. Each meta-tool is therefore folded into the total exactly once,
# regardless of how many surfaces are sampled.
META_TOOLS = {"search_tools", "describe_tool", "run_tool", "context_cost"}


class _NeverCalled:
    """Stub executor for the gcal sample: only tool DEFINITIONS are measured,
    never invoked, so `run` is never entered."""

    async def run(self, verb, args):
        raise AssertionError("calibration measures definitions, not calls")


def _gcal_backend() -> Backend:
    return Backend(
        name="gcal",
        kind="native",
        descriptors=descriptors_for(pinned=VERBS),
        executor=_NeverCalled(),
    )


async def _sample(enc, name: str, mcp: FastMCP) -> list[dict]:
    samples = []
    for tool in await mcp.list_tools():
        text = tool.to_mcp_tool().model_dump_json(exclude_none=True)
        samples.append(
            {
                "surface": name,
                "name": tool.name,
                "chars": len(text),
                "tokens": len(enc.encode(text)),
            }
        )
    return samples


async def main() -> None:
    enc = tiktoken.get_encoding("cl100k_base")
    cmd = f"python {Path(__file__).parent.parent / 'tests/fixtures/fake_tool.py'}"
    faketool = load_cli_backend(CliBacking(name="faketool", cmd=cmd))

    samples = []
    samples += await _sample(enc, "faketool", build_surface(faketool))
    samples += await _sample(enc, "gcal", build_surface(_gcal_backend()))

    surfaces = {}
    for s in samples:
        agg = surfaces.setdefault(s["surface"], {"chars": 0, "tokens": 0, "tools": 0})
        agg["chars"] += s["chars"]
        agg["tokens"] += s["tokens"]
        agg["tools"] += 1
    for agg in surfaces.values():
        agg["chars_per_token"] = round(agg["chars"] / agg["tokens"], 3)

    # Dedupe meta-tools for the TOTAL only -- keep the first occurrence of each
    # (from whichever surface samples it first) and every non-meta tool from
    # every surface. Per-surface `surfaces` aggregates above are left as-is:
    # they describe what that one surface actually publishes, which really
    # does include its own copy of each meta-tool.
    seen_meta: set[str] = set()
    deduped = []
    for s in samples:
        if s["name"] in META_TOOLS:
            if s["name"] in seen_meta:
                continue
            seen_meta.add(s["name"])
        deduped.append(s)

    total_chars = sum(s["chars"] for s in deduped)
    total_tokens = sum(s["tokens"] for s in deduped)
    FIXTURE.write_text(
        json.dumps(
            {
                "encoding": "cl100k_base",
                "generated_by": "scripts/calibrate_tokens.py",
                "surfaces": surfaces,
                "samples": samples,
                "total_chars": total_chars,
                "total_tokens": total_tokens,
                "chars_per_token": round(total_chars / total_tokens, 3),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"chars_per_token = {total_chars / total_tokens:.3f}  ({len(deduped)} tools, deduped)")
    for name, agg in surfaces.items():
        print(f"  {name}: {agg['chars_per_token']:.3f}  ({agg['tools']} tools)")


asyncio.run(main())

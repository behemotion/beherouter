"""What a surface costs a client's context, measured once and read four ways.

The house pattern from `indexing.py` ("the single place descriptors become
search-index entries") and `pluginconfig.py` ("emits all three plumbing
fragments from one spec, so they cannot disagree"). One function computes; the
MCP tool, the CLI verb, `health --deep` and the surface's `instructions` string
all read it.

WHY AN ESTIMATE. An exact count needs a tokenizer, and every real one either
downloads a vocabulary over the network on first use (tiktoken) or drags in a
model runtime. The gateway runs rootless in an egress-restricted container where
the former fails closed, and AGENTS.md's no-embeddings constraint exists to keep
that class of dependency out. So the count is a calibrated heuristic, reported
as `exact: false` with the method named, and never dressed up as authoritative.

WHY THE BUILT SURFACE, NOT THE DESCRIPTORS. `FunctionTool.to_mcp_tool()
.model_dump_json(exclude_none=True)` is byte-for-byte what goes on the wire.
Measuring descriptors instead would mean reimplementing FastMCP's
signature-to-inputSchema synthesis, and a reimplementation drifts. Because all
four readers measure the same built surface, their agreement is structural
rather than a discipline someone has to maintain.
"""

from dataclasses import dataclass, replace
from math import ceil

from .errors import UsageError
from .models import Backend, ToolDescriptor

# Calibrated by scripts/calibrate_tokens.py against real published tool
# definitions (a CLI backend's own tools plus beherouter's meta-tools, AND the
# calendar plugin's six schema-dense tools) and pinned by
# tests/fixtures/token_calibration.json. MCP tool definitions are a mix of
# schema-dense JSON Schema bodies and English-prose descriptions rather than
# either shape alone, so the measured figure -- not a guess about punctuation
# density -- is what this constant records.
#
# Rounded DOWN from the measured value on purpose: a smaller divisor reports
# MORE tokens, and over-reporting cost is the safe direction. Under-reporting
# invites a host to pin more than its context window can afford.
CHARS_PER_TOKEN = 4.3

METHOD = f"estimate/json-{CHARS_PER_TOKEN}"

# The meta-tools every surface publishes. Their cost is overhead the gateway
# adds; a direct connection to the backend would not pay it.
META_TOOL_NAMES = frozenset(
    {"search_tools", "describe_tool", "run_tool", "context_cost"}
)


def estimate_tokens(text: str) -> int:
    """Tokens in `text`, estimated. Never fewer than 1 for non-empty input."""
    if not text:
        return 0
    return max(1, ceil(len(text) / CHARS_PER_TOKEN))


def tool_tokens(tool) -> int:
    """Cost of one published tool definition, as serialized on the wire."""
    return estimate_tokens(tool.to_mcp_tool().model_dump_json(exclude_none=True))


@dataclass(frozen=True)
class SurfaceCost:
    advertised: int  # tools in the backend's catalogue
    pinned: int  # tools in the published array
    tokens_meta: int  # search_tools + describe_tool + run_tool + context_cost
    tokens_pinned: int
    tokens_published: int  # == tokens_meta + tokens_pinned; what a host pays
    tokens_naive: int  # every tool published, no meta-tools: a direct connection
    tokens_saved: int  # tokens_naive - tokens_published; MAY BE NEGATIVE
    method: str
    exact: bool = False


async def naive_tokens(backend: Backend) -> int:
    """What connecting straight to this backend would cost.

    Builds a throwaway surface with every descriptor pinned and no meta-tools,
    through the same `register_pinned` the real surface uses, so the comparison
    is against the shape beherouter actually serves.
    """
    from fastmcp import FastMCP

    from .surface import register_pinned

    # `replace`, not a hand-listed constructor: a field added to `Backend`
    # later would be silently dropped here and nowhere else in this module.
    everything = replace(
        backend, descriptors=[replace(d, pinned=True) for d in backend.descriptors]
    )
    bare = FastMCP(f"{backend.name}__naive")
    for d in everything.descriptors:
        register_pinned(bare, d, everything)
    return sum(tool_tokens(t) for t in await bare.list_tools())


async def surface_cost(
    mcp, backend: Backend, descriptors: list[ToolDescriptor] | None = None
) -> SurfaceCost:
    """Measure a built surface. `mcp` is the FastMCP returned by build_surface.

    `descriptors` overrides `backend.descriptors` for BOTH `advertised` and the
    `tokens_naive` baseline. `context_cost` passes the live `Catalogue`'s fresh
    list: after a refresh the Catalogue holds the fresh list while
    `backend.descriptors` still holds the attach-time one, and the two numbers
    must come from the same set -- `naive_tokens`'s own contract is "every tool
    published... a direct connection", i.e. exactly the advertised set. Taking
    `advertised` from one catalogue and `tokens_naive` from another would make
    the payload self-contradictory: a fresh count next to a stale cost.
    """
    fresh = backend.descriptors if descriptors is None else descriptors
    published = await mcp.list_tools()
    meta = sum(tool_tokens(t) for t in published if t.name in META_TOOL_NAMES)
    pinned = sum(tool_tokens(t) for t in published if t.name not in META_TOOL_NAMES)
    naive = await naive_tokens(replace(backend, descriptors=fresh))
    return SurfaceCost(
        advertised=len(fresh),
        pinned=len(backend.pinned),
        tokens_meta=meta,
        tokens_pinned=pinned,
        tokens_published=meta + pinned,
        tokens_naive=naive,
        tokens_saved=naive - (meta + pinned),
        method=METHOD,
    )


def as_payload(cost: SurfaceCost, context_window: int | None = None) -> dict:
    """The wire shape all four readers emit.

    `context_window` is an INPUT because beherouter never sees the host's
    context window. The doc's threshold guidance ("a percentage of the context
    window, for example 1%-5%") is the host's decision; beherouter supplies the
    numerator and states the cost, and issues no verdict on someone else's
    budget.
    """
    payload = {
        "advertised": cost.advertised,
        "pinned": cost.pinned,
        "tokens_meta": cost.tokens_meta,
        "tokens_pinned": cost.tokens_pinned,
        "tokens_published": cost.tokens_published,
        "tokens_naive": cost.tokens_naive,
        "tokens_saved": cost.tokens_saved,
        "method": cost.method,
        "exact": cost.exact,
    }
    if context_window is None:
        return payload
    if context_window <= 0:
        raise UsageError(f"context_window must be positive, got {context_window}")
    payload["pct_published"] = 100 * cost.tokens_published / context_window
    payload["pct_naive"] = 100 * cost.tokens_naive / context_window
    return payload


def instructions_line(cost: SurfaceCost, surface: str) -> str:
    """One sentence for a surface's `instructions`, read by a host at
    `initialize` -- so the cost is known WITHOUT spending a tool call."""
    tail = ""
    if cost.advertised > cost.pinned:
        tail = (
            f" The other {cost.advertised - cost.pinned} are reachable through "
            f"search_tools; publishing all {cost.advertised} would cost about "
            f"{cost.tokens_naive} tokens."
        )
    elif cost.advertised == cost.pinned:
        tail = (
            f" All {cost.advertised} are published; search_tools is available "
            f"but adds no context saving on this surface."
        )
    else:
        # advertised < pinned. Unreachable at attach, where the published set is
        # a SUBSET of the catalogue -- but the published array is frozen for the
        # process lifetime while the catalogue refreshes, so a post-drift cost
        # can land here with a pinned tool the backend no longer serves. Saying
        # "all N are published" there would assert something false about a
        # surface that is in fact publishing a broken tool.
        tail = (
            f" {cost.pinned - cost.advertised} published tool(s) are no longer "
            f"in the backend's catalogue; see `beherouter health --deep`."
        )
    return (
        f"Surface '{surface}': {cost.pinned} of {cost.advertised} tools published, "
        f"costing about {cost.tokens_published} tokens ({cost.method}).{tail}"
    )

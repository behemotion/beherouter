# MCP client-best-practices conformance — design

**Date:** 2026-09-10
**Status:** design approved in chat; **nothing implemented.**
**Source:** `https://modelcontextprotocol.io/docs/2026-07-28/develop/clients/client-best-practices`
**Measured against:** `fastmcp 3.4.5`, `mcp 1.29.0` (the versions in `uv.lock` on this date).

## Which half of the doc binds us

`client-best-practices` is written for MCP **hosts** — the thing that drives the
model. beherouter is neither a pure host nor a pure server: it is an MCP **client**
to backends and an MCP **server** to hosts. The doc therefore splits three ways:

| Bucket | Binding? |
|---|---|
| Practices we implement **on behalf of** hosts — progressive discovery | **Yes.** This is the mission: LibreChat and Hermes have no native tool search. |
| Practices that bind us **as a client of backends** — metadata, caching, `list_changed`, error handling | **Yes.** |
| Practices that belong to the host — sandbox / code mode, context-window thresholds, prompt-cache breakpoint management | **No.** We never see the model's context. |

The audit that produced this spec found the third bucket correctly out of scope and
the first bucket already **fully satisfied** — the three-layer pattern
(`search_tools` → `describe_tool` → `run_tool`, `surface.py:199,214,228`) is the
doc's canonical shape, BM25 is a first-class strategy in the doc rather than a
fallback, and `search_tools` already returns descriptions inline so Layer 2 is
often skippable. This design is about the second bucket, plus one thing the doc
asks for that we cannot do ourselves and must therefore **measure and advertise**.

## Problem

Five concrete gaps, all instances of one root cause, plus one absence.

### The root cause: a backend's catalogue is treated as inert data read once

`backend_from_client` (`backends/mcp.py:123`) calls `list_tools()` at attach and
never again. Everything the backend says about its tools beyond name, description
and input schema is dropped on the floor, and nothing can ever change it.

1. **`mutating` is hardcoded `False` for every MCP backend** — `backends/mcp.py:131`.
   All 30 of `plane`'s tools, deletes and archives included, are advertised to the
   model as non-mutating through `search_tools` and `describe_tool`. `cli` backends
   read it from the beheaxi manifest (`backends/cli.py:195`) and the calendar
   plugins compute it from a `MUTATING` set (`plugins/calendar/tools.py:155`), so
   this is an MCP-backing-only hole. `mcp.types.Tool` carries `annotations`
   (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) — we are
   discarding data the SDK hands us.

2. **Tool annotations are never republished** — `surface.py:191` registers each
   pinned tool as `mcp.tool(name=..., description=...)` and nothing else. A host
   that never calls `search_tools` — Claude Code, which sees pinned tools directly
   — gets no mutation signal at all. This is the doc's Security Considerations
   concern: the host's human-in-the-loop confirmation policy keys off exactly
   these hints.

3. **No `list_changed` handling in either direction.** `grep -rn "list_changed"
   src/ tests/` returns nothing. The doc's guideline is *"Refresh on
   `list_changed` — re-index the search catalog when a server sends
   `notifications/tools/list_changed`."* The BM25 index is built once in
   `build_surface` and never rebuilt. This compounds a trap already documented in
   AGENTS.md — *"a backend's catalogue advertises the commercial surface... re-probe
   the whole pin list after an upgrade or an edition change"* — because nothing
   currently tells us the catalogue moved.

4. **Cache hints (`ttlMs` / `cacheScope`) unimplemented.** We cache for a
   configured TTL (this plan's `catalogue_ttl_ms`), not indefinitely — but that
   TTL is fixed and ours, not read from the backend. Ignoring a hint means we
   may cache *longer* than a backend would like, not shorter: we are not
   over-trusting stale data in some verified sense, we are ignoring an
   invalidation signal.

5. **`outputSchema` is stripped** — `models.py` has no field for it and every
   result is wrapped as `{"result": ...}` (`backends/mcp.py:94,115`). The doc is
   explicit that a missing output schema forces hosts onto a degraded path: generic
   `any` typing, or a fast-model `extract()` helper that *"adds per-call latency and
   can hallucinate or drop fields."* We sit between backends that have the schema
   and hosts that want it, and drop it in transit.

### The separate one: tool errors are misclassified as outages

`backends/mcp.py:93,114` catches bare `Exception` and raises `Unavailable`. FastMCP
raises `fastmcp.exceptions.ToolError` when a backend returns `isError: true` —
`Client.call_tool` defaults to `raise_on_error=True` and its own docstring states
*"Unlike call_tool_mcp, this method raises a ToolError if the tool call results in an
error"* (verified against 3.4.5) — the
doc's Error Handling case, a *successful* MCP response the model should self-correct
against. Collapsing it into `Unavailable` puts a bad-argument error in the same class
as a dead subprocess. `Unavailable` is a CONVENTIONS-level classification with a
reserved exit code, so a backend `UsageError` arriving as `Unavailable` reads to any
operator, monitor or `health --deep` consumer as a backend outage.

### The absence: nothing measures what a surface costs

The doc's threshold guidance — *"Implement a threshold as a percentage of the
context window. For example, 1%-5%"* — is a host decision, and beherouter never sees
the host's context window. But the **numerator** is ours alone: only the gateway
knows how many tokens its published tool set costs, and only the gateway can compare
that against what connecting straight to the backend would cost. Today nobody knows.
AGENTS.md asserts the shape of the answer in prose — *"`office` is a pass-through...
the benefit here is credential centralisation and one uniform client surface, **not**
context savings"* — and no test enforces it.

## The doc contradicts itself for our architecture; here is the seam

Two guidelines pull opposite ways:

> **Refresh on `list_changed`** — re-index the search catalog when a server sends
> `notifications/tools/list_changed`.

> **Interaction with Prompt Caching** — Adding or removing tool definitions
> mid-conversation invalidates that cache... Treat server disconnection as a
> conversation-boundary operation rather than a per-turn one.

The current frozen-at-attach catalogue satisfies the second by violating the first.

**The seam:** the *searchable* catalogue — the BM25 index, `describe_tool`'s lookup
and `run_tool`'s routing — lives entirely behind meta-tools and is invisible to the
published `tools` array. Only the *pinned* set is array-visible. So:

- The searchable catalogue refreshes freely. Nothing a host caches is touched.
- The pinned set stays frozen for the process lifetime. The tools array remains
  byte-stable, which is what the caching guideline actually protects.
- Divergence between the two — a pinned tool that vanished, new tools that appeared
  — is **reported as drift**, not silently reconciled.

This is strictly better than the status quo on both guidelines at once.

## Decisions settled 2026-09-10

1. **Context cost is advertised four ways** from one measurement core: an MCP tool,
   a CLI verb, `health --deep` records, and the surface's `instructions` string.
2. **Token counting is a dependency-free heuristic**, reported as an estimate.
   `tiktoken` is rejected: it fetches its BPE vocab over the network on first use,
   which fails closed in the gateway's egress-restricted rootless container and
   would need a vendored blob in the homelab Containerfile.
3. **Refresh splits**: index refreshes on TTL, pins stay frozen, drift is a health
   verdict. No persistent notification session.
4. **`outputSchema` is republished wrapped**, preserving the `{"result": ...}`
   envelope. No consumer breaks.
5. **`mutating` becomes tri-state.** Unknown is a third value, not a default.
6. **Backend tool errors map to `UsageError`, uniformly, with no message sniffing.**

## Architecture

### `src/beherouter/costing.py` — one measurement, four readers

The house pattern from `indexing.py` (*"the single place descriptors become
search-index entries... they built the index independently until 2026-08-04, which
meant enriching one silently diverged the two"*) and `pluginconfig.py` (*"emits all
three plumbing fragments from one spec, so they cannot disagree"*). One function
computes; four consumers read.

```python
def estimate_tokens(text: str) -> int:
    """Tokens in `text`, estimated.

    MCP tool definitions are punctuation-dense JSON, not English prose, so the
    familiar 4.0 chars/token figure over-counts badly. CALIBRATE the divisor
    against the real plane/office catalogues before shipping (see Open questions).
    """

def tool_cost(d: ToolDescriptor) -> int:
    """Cost of ONE published tool definition.

    Measures what beherouter actually publishes — name, description, inputSchema,
    annotations, outputSchema — serialized as compact JSON, NOT the backend's
    original definition. The two differ (we wrap output schemas, we omit unknown
    annotations), and the number must describe what a host really pays.
    """

@dataclass(frozen=True)
class SurfaceCost:
    advertised: int          # tools in the catalogue
    pinned: int              # tools in the published array
    tokens_meta: int         # search_tools + describe_tool + run_tool + context_cost
    tokens_pinned: int
    tokens_published: int    # == tokens_meta + tokens_pinned; what a host pays
    tokens_naive: int        # every tool pinned, no meta-tools; a direct connection
    tokens_saved: int        # tokens_naive - tokens_published; MAY BE NEGATIVE
    # `method` names the divisor actually shipped, e.g. "estimate/json-3.2", so a
    # payload is self-describing and a recalibration is visible to consumers.
    method: str
    exact: bool = False
```

**`tokens_saved` may be negative, and that is the point.** `office` publishes 4
pinned tools plus 4 meta-tools, so the gateway costs a host *more* context than a
direct connection would. AGENTS.md already claims this in prose; this makes it a
number, and a test asserts the sign in both directions (negative for `office`,
positive for `plane`).

**Threshold support.** The doc's 1%–5% guidance needs a context window we do not
have, so the window is an **input**:

```python
def as_payload(cost: SurfaceCost, context_window: int | None = None) -> dict
```

With a window supplied, the payload adds `pct_published` and `pct_naive`; the host
applies its own threshold. Omitted, the payload carries raw counts only. beherouter
states the cost and never issues a verdict on someone else's budget.

**Cost requires attach.** For `http`, `stdio` and `cli` backings the catalogue only
exists after connecting; only `native` plugins could be costed inertly. Special-casing
them would make one command mean two different things, so the CLI verb attaches — the
same choice `search` and `health --deep` already make. "Check every plugin" therefore
means "check every attached surface"; costing an unattached plugin is out of scope.

### The four readers

| Reader | Shape |
|---|---|
| **MCP tool** | `context_cost(context_window: int \| None = None)` — a 4th meta-tool on every surface. The model can ask what the surface costs it. Adds ~1 tool definition per surface, and that definition is itself counted in `tokens_meta`. |
| **CLI** | `beherouter context-cost [--surface S] [--context-window N]` — iterates the registry, attaches each entry, emits one record per surface. Same shape as `deep_health`: reports rather than raises, so one dead backend does not hide the others. |
| **`health --deep`** | Each record gains `advertised` and `tokens_published`, so a backend upgrade that grows the catalogue shows up in the monitor. |
| **Surface `instructions`** | `FastMCP(name, instructions=...)` carries one sentence read at `initialize`, so a host learns the cost with **no tool call at all**. Per-tool token counts in `meta` were considered and dropped: a count stored inside a tool's own definition changes the size it reports. |

The `instructions` string is computed from the frozen pinned set at `build_surface`
time and is therefore static — it cannot drift from the published array, because the
published array cannot move either.

### `src/beherouter/catalogue.py` — refresh the index, freeze the pins

```python
@dataclass(frozen=True)
class Drift:
    added: list[str]
    removed: list[str]
    pinned_missing: list[str]   # a pinned tool the backend no longer serves

    @property
    def changed(self) -> bool: ...

class Catalogue:
    """Descriptors + BM25 index + freshness, for one backend."""
    async def fresh(self) -> "Catalogue"   # re-lists iff stale, else self
    def diff(self, other) -> Drift
```

`Backend` gains two fields:

```python
relist: Callable[[], Awaitable[list[ToolDescriptor]]] | None = None
ttl_ms: int | None = None
```

`cli` and `native` backings pass `relist=None` → never stale → **zero behavioural
change** for `gcal`, `m365` and every beheaxi backend. Only `mcp` backings refresh.

`surface.py`'s three meta-tools read through `Catalogue.fresh()`. The pinned tools
registered on the FastMCP instance are never re-registered. `run_tool` resolving a
name that appeared *after* attach is the intended win: the long tail refreshes while
the array does not.

**A refresh failure is not an outage.** If the backend cannot be re-listed, the
Catalogue keeps serving the last good descriptors and records the failure. Making a
transient re-list error take down `search_tools` would trade a stale index for a dead
surface — the wrong direction, and the same instinct behind `/healthz` deliberately
not fanning out to backends (`gateway.py:healthz`).

**Cache hints.** `ListToolsResult` in `mcp` 1.29.0 has exactly `meta`, `nextCursor`
and `tools` — no typed `ttlMs` or `cacheScope`. The 2026-07-28 caching utility is not
in the SDK. Hints could only arrive through `_meta`, and reading them requires
`list_tools_mcp()` because `Client.list_tools()` discards the envelope and returns
`list[Tool]`. That means owning the cursor-pagination loop FastMCP currently gives us
free (`max_pages=250`, duplicate-cursor detection). **Phase 1 therefore uses a
configured TTL only**; hint-reading is Phase 2, gated on a backend in this harness
actually emitting one. See Delivery phases.

### Registry surface

One new optional key, per entry and with a plugin-level default:

```toml
[plane]
plugin = "plane"
catalogue_ttl_ms = 300000    # 0 disables refresh entirely
```

Defaults: `PluginSpec` gains `catalogue_ttl_ms: int = 300_000` so a plugin ships a
tested value, and `RegistryEntry` gains `catalogue_ttl_ms: int | None = None` so an
entry overrides it — the same override-of-a-tested-default shape
as `pinned` and `probe`. `registry-lint` validates the type and non-negativity
offline.

### Metadata passthrough

`ToolDescriptor` gains:

```python
annotations: dict | None = None      # raw MCP annotations, as the backend sent them
output_schema: dict | None = None
mutating: bool | None                # WIDENED from bool
```

Ingest, in `backend_from_client`:

```python
ann = getattr(t, "annotations", None)
mutating = None if ann is None else not getattr(ann, "readOnlyHint", False)
```

**Tri-state, and unknown is not a default.** The precedent is `health.py`, where
`PROBE_NONE` exists because *"no probe configured -> UNKNOWN, never green"* and
`failed()` documents *"`none` is NOT a failure — it is an absence of evidence, and
conflating the two would make an unprobed gateway alarm forever."* An unannotated
backend tool is the identical epistemic situation. `False` is today's bug; `True`
would make every read tool on an unannotated backend look destructive — the same
alarm-forever failure, mirrored.

Republish, in `surface.py:191`:

```python
mcp.tool(
    name=d.name,
    description=d.summary,
    annotations=_republished_annotations(d),   # None when nothing is known
    output_schema=_wrapped_output_schema(d),   # None when the backend declared none
)(_make_pinned_tool(d, backend))
```

`_republished_annotations` emits `readOnlyHint` **only when known**, so beherouter
never invents an annotation a backend did not make. Other hints (`destructiveHint`,
`idempotentHint`, `openWorldHint`) pass through verbatim when present.

`_wrapped_output_schema` preserves the envelope:

```python
{"type": "object", "properties": {"result": <backend schema>}, "required": ["result"]}
```

The wire format is unchanged — every consumer still reads `.result`. FastMCP validates
tool output against a declared schema, so the wrapper is what makes the declaration
true rather than a lie that fails validation on the first call.

`search_tools` and `describe_tool` payloads gain `annotations`; `mutating` may now be
`null`, which is a payload change consumers must tolerate.

### Error classification

```python
from fastmcp.exceptions import ToolError

try:
    res = await client.call_tool(verb, args)
except ToolError as e:
    # The backend answered isError:true. A tool-level error, not an outage.
    raise UsageError(f"backend rejected '{verb}': {e}") from e
except Exception as e:
    # Transport, dead subprocess, closed socket.
    raise Unavailable(f"backend call '{verb}' failed: {e}") from e
```

Applies to both `MCPClientExecutor` and `ReconnectingMCPExecutor`. The backend's own
message is preserved verbatim, which is the doc's stated purpose: *"surface it as the
script's result so the model can self-correct."*

**No message sniffing.** We do not inspect the text for "auth" or "not found" to pick
`AuthError` or `NotFound`. Backend error strings are unstructured and a heuristic here
would misclassify silently — the failure mode this repo has been bitten by twice
(the `{"result": null}` masking in `_payload`, the action-parameterized default
forwarding in `_make_pinned_tool`).

**Health is unaffected and must be shown to be.** `check_entry` catches `AxiError`,
the common base of `UsageError` and `Unavailable`, so a failing probe is still
`PROBE_FAILED`. The 2026-07-30 revoked-PAT scenario — the reason deep health exists —
keeps being caught. A regression test asserts it.

### Catalogue drift as a health verdict

`check_entry` gains a `catalogue` axis alongside `attach` and `probe` — but a
narrowed one, reporting only the two values it can actually observe:

```python
{"name": "plane", "attach": "ok", "probe": "ok",
 "catalogue": "pinned_missing", "pinned_missing": ["workitem_type"],
 "advertised": 32, "tokens_published": 3140}
```

This example spans two phases: `advertised` and `tokens_published` land in Phase 2,
the `catalogue` axis in Phase 3. Neither depends on the other — the record grows
twice, and each phase's tests assert only its own fields.

`health --deep` reports two values on this axis: `ok` and `pinned_missing`.
`drift` and `stale` are states of a **live** `Catalogue` and are not
observable from `check_entry`, which attaches fresh in a separate process
with no baseline to diff against — a black-box property that is deliberate.
The live status rides on the `context_cost` payload instead — e.g.
`{"catalogue": "drift", "added": ["cycle_x"], "removed": []}` alongside
`advertised`/`tokens_published`, a shape distinct from `check_entry`'s output
above and produced by the live `Catalogue`, not by a health check.

**`drift` is not a failure**, for the same reason `PROBE_NONE` is not: a backend
gaining tools is normal. **`pinned_missing` IS a failure** and joins `failed()` — a
pinned tool the backend no longer serves is a broken published tool, which is exactly
the "the tool exists in the catalogue but not in this deployment" trap AGENTS.md warns
about after an edition change or upgrade. This is the mechanical version of the
formerly-manual rule *"re-probe the whole pin list after an upgrade or an edition
change"* — mechanized for the existence half; `probe` still covers whether a served
tool actually works.

## Testing

- **Estimator calibration** against a committed fixture of real MCP tool-definition
  JSON with hand-recorded token counts. No network. Asserts the divisor is within
  tolerance rather than asserting an exact count.
- **`tokens_saved` sign**: negative for an `office`-shaped surface (all tools pinned,
  meta-tools are pure overhead), positive for a `plane`-shaped one. This is the test
  that turns a AGENTS.md prose claim into an enforced property.
- **The four readers agree**: the MCP tool, the CLI verb, the health record and the
  `instructions` string all report the same `tokens_published` for one backend.
  Divergence here is the exact failure `indexing.py` was created to prevent.
- **Refresh**: a fake `relist` returning a changed catalogue rebuilds the index and
  makes a new tool findable via `search_tools`, while `await mcp.get_tools()` is
  byte-identical before and after. This is the caching-property test.
- **Refresh failure** serves the last good catalogue and reports `stale`.
- **No behaviour change for non-mcp backings**: `cli` and `native` backends assert
  `relist is None` and never re-list.
- **Annotations round-trip**, including the unannotated case → `mutating is None` and
  no invented `readOnlyHint` on the republished tool.
- **Output schema wrapping** validates against a real call's payload.
- **Error split**: `ToolError` → `UsageError`, transport failure → `Unavailable`, and
  `check_entry` still reports `PROBE_FAILED` for both.
- **`pinned_missing` fails `failed()`; `drift` alone does not.**
- **`registry-lint`** rejects a negative or non-integer `catalogue_ttl_ms` offline.

## Deliberately out of scope

- **Programmatic tool calling / code mode.** We are not the host: there is no model
  here to write code and no place to run it. Propagating `outputSchema` is the whole
  of our obligation — it is what lets a host that *does* implement code mode generate
  precise types instead of falling back to `any`.
- **Embeddings.** Standing constraint; the doc endorses keyword search as a
  first-class strategy, not a fallback.
- **A `list_changed` notification listener.** Rejected in favour of TTL: a long-lived
  notification-only session per backend reintroduces exactly the stale-socket and
  dead-subprocess fragility `ReconnectingMCPExecutor` was written to avoid, and a
  dead listener fails silently — no error, just a gateway that quietly stops
  refreshing.
- **Emitting `notifications/tools/list_changed` downstream.** Would break the
  prompt-cache stability the split in this design exists to protect.
- **Re-pinning at runtime.** Drift is reported; reconciling it is an operator action
  and a deploy, in line with "the pin list is a tested plugin default."
- **Lazy per-surface attach.** It would satisfy the doc's Dynamic Server Management
  section and incidentally fix the crash-loop trap, but it is a separate design with
  its own failure modes. Noted for a future brainstorm, not smuggled in here.
- **Costing an unattached plugin.** See "Cost requires attach."

## Open questions the implementer must settle by measurement

1. **The chars-per-token divisor.** The 3.2 figure in the brainstorm is a guess.
   Measure it against the real `plane` (30 tools) and `office` (4 tools) catalogues
   before committing a constant, and record the calibration in the fixture. If JSON
   schema text turns out to vary widely by backend, prefer a slightly conservative
   (over-counting) divisor: over-reporting cost is safe, under-reporting invites a
   host to pin more than it can afford.
2. **The default `catalogue_ttl_ms`.** 300 000 ms is a proposal, not a measurement.
   Check `plane`'s stdio re-list latency: if a re-list is expensive enough to be felt
   inside a `search_tools` call, the refresh must move off the request path (a
   background task, or refresh-after-respond) and that is a design change worth
   flagging before implementing.
3. **Whether any harness backend emits `_meta` cache hints at all.** If none do,
   Phase 2 stays unbuilt and the TTL is the whole mechanism.
4. **Whether `plane`'s catalogue actually drifts** across a Community Edition
   upgrade. If it does, the `pinned_missing` failure path has a real regression
   fixture; if it does not, the test stays synthetic.

## Delivery phases

**Phase 1 — metadata and errors** (no new modules, no behaviour change to refresh):
`ToolDescriptor` widening, annotation ingest and republish, wrapped `output_schema`,
the `ToolError`/`Unavailable` split, and the health regression test. Independently
shippable and independently valuable: it closes the security-relevant gap (a
destructive tool advertised as read-only) with no moving parts.

**Phase 2 — costing**: `costing.py` and its four readers. Depends on Phase 1 only
because `tool_cost` must measure the *published* definition, which Phase 1 changes.

**Phase 3 — refresh**: `catalogue.py`, `Backend.relist`, `catalogue_ttl_ms`, drift in
health. The largest phase and the only one that touches the request path.

**Phase 4 — cache hints** (conditional): `list_tools_mcp()` + manual pagination,
`_meta` hint reading. Gated on Open question 3. If no backend emits hints, this phase
is closed unbuilt and the divergence is recorded in `HARNESS-DIVERGENCES.md` as a
deliberate, reasoned gap rather than an oversight.

## Consequences for the harness

- `HARNESS-DIVERGENCES.md` gains an entry for Phase 4 if it closes unbuilt.
- ~~AGENTS.md's Plugins table gains the per-surface token cost once Phase 2 lands,
  replacing "11 pinned / 30 advertised" with the same counts plus what they cost.~~
  **Struck 2026-09-11, unbuilt.** A hand-copied token figure in AGENTS.md goes
  stale the moment a backend's catalogue drifts, and nothing keeps it in sync —
  whereas `beherouter context-cost` and each surface's `instructions` string
  (read by a host at `initialize`, so it costs no tool call) both report the
  live number. The table keeps the counts, which are configuration and do not
  move on their own.
- `BEHEMOTION/docs/CONVENTIONS.md` is **not** changed by this design. Nothing here alters the
  attach contract, the error envelope's meaning, or the naming rules — Phase 1
  narrows which envelope a given failure gets, which is a correction toward the
  existing convention, not a change to it.

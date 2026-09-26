# Plugin sources — in-process providers, OpenAPI, catalog import, maturity — design

**Date:** 2026-09-25
**Status:** design written from the 2026-09-25 plugin-system audit; **nothing
implemented.** Two spikes were run against the locked FastMCP (3.4.5) and are
quoted below as measurements, not assumptions.
**Scope:** a fifth way to back a surface — an **in-process FastMCP server** —
and three sources built on it (`openapi`, `python-dir`, and the existing `native`
re-expressed); generic `mcp-http` / `mcp-stdio` plugins; `beherouter catalog
import` from an MCP `server.json`; and a declared, **evidenced** maturity tier
per plugin.
**Release:** minor on explicit ask only (AGENTS.md § Versioning). As phased
below, phases 0–2 are each shippable as a patch.

## The motto this serves

> **To backends:** pluggable, pre-configured MCP servers — every backend is a
> versioned, tested plugin carrying its own pins, probe, config schema,
> credential names and quirk workarounds, so attaching one is a name in
> `registry.toml`, not an act of archaeology.

That half is true for the backends *we* curated. It is not true for a
customer's: the only way to attach anything is to write a Python package, and
the only way to expose a REST API or a Python function is to write a `native`
plugin by hand.

## Problem — found by the 2026-09-25 audit

### 1. A customer cannot attach their own tools without becoming a plugin author

| They have | What it takes today |
|---|---|
| an MCP server URL | a plugin package with an entry point, published to an index, installed, gateway restarted. `url`/`cmd` were removed from `registry.toml` on purpose (the `gitea-home` lesson) and nothing replaced them for the uncurated case |
| an internal REST API with an OpenAPI spec | an MCP server of their own, then the above. `from_openapi` appears nowhere in `src/` |
| a Python function | a `native` plugin: hand-written JSON Schema, a `ToolDescriptor` per tool, an `Executor` class that validates arguments and wraps every stray exception, and a `Backend` — **≈ 50–60 lines for one tool**; the calendar core is ≈ 470 |

Every serious gateway surveyed (agentgateway, LiteLLM, Unla, Kong, Docker MCP
Gateway, ContextForge) accepts a **declarative** entry for these cases and uses
code only for middleware. None asks for code to attach an MCP server or an
OpenAPI spec.

### 2. `native` is the expensive backing, and it is the one in-process sources need

`build_transport` knows `stdio` and `http` only (`backends/mcp.py:77-80`).
Everything that is not an MCP server over a wire must reimplement what
`backend_from_client` already does for MCP servers: descriptors, annotations →
`mutating`, output schemas, notes, re-listing.

### 3. Nothing says how far a plugin has been proven

`office-mcp` has production traffic and a probe; `plane-http` is proven
per-user in `tests/e2e/`; `gcal` is tested but has never attached. `beherouter
plugins` prints all three identically. Adding imported and generic plugins
without a signal would put an unprobed `mcp-http` entry next to `plane` with
nothing to tell an operator which is which — the state `gitea-home` was in.

## Measured 2026-09-25 (spikes, FastMCP 3.4.5)

A two-operation OpenAPI 3.0 spec served by `FastMCP.from_openapi(spec,
client=httpx.AsyncClient(..., event_hooks={"request": [inject]}))`, where
`inject` copies headers from a `contextvars.ContextVar` onto the outbound
request, and the attach-time client carries `authorization: Bearer deploy`:

| Path | Per-call identity reached the upstream request | Latency (mean) |
|---|---|---|
| `Client(FastMCPTransport(server))`, a session per call | yes — three concurrent calls saw `alice`, `bob`, `deploy` respectively | 8–14 ms |
| `await server.call_tool(name, args)`, no session | yes — `alice`, `deploy` | **0.87 ms** |

- A context variable set by the caller reaches the provider's httpx hook on
  **both** paths, and concurrent calls do not bleed into each other.
- The direct path returns a `ToolResult` (`structured_content`, `content`), which
  `_payload` already reads.
- `server.list_tools()` returns the same tools as a `tools/list` over the
  transport.

## Approach

**One seam, many sources.** Add an `inproc` transport to `McpBacking`: the
plugin's `build()` constructs a `FastMCP` server (from OpenAPI, from a
directory, from decorated functions) and hands it over. Listing reuses
`backend_from_client` over an in-memory transport, so an in-process tool gets
**exactly** the descriptor pipeline every MCP backend already has — pins,
annotations, notes, output schemas, search indexing, TTL re-list. Calls go
direct (`server.call_tool`), which is 10× cheaper and is the one place per-call
identity is applied.

Rejected alternatives:

- **Run OpenAPI/python-dir as a sidecar MCP server over http.** Works today with
  no gateway change, and is what a customer can already do. It costs a container
  per source, and it is the "you must stand up an MCP server" barrier this
  design exists to remove.
- **A declarative REST YAML (Unla/Airbyte style) as the first source.** Strictly
  more to build and maintain than `OpenAPIProvider`, which FastMCP already
  ships; most internal APIs have or can emit an OpenAPI document. Deferred, not
  refused (§ Out of scope).
- **Grow `native` instead.** `native` stays for what it is good at — plugins
  that own a credential lifecycle, like the calendars' refresh-token providers.
  Rewriting the calendars onto `inproc` is possible later and is not part of
  this design.

## Design

### Phase 0 — prerequisites (patch)

1. **Cap FastMCP: `fastmcp>=3.4,<4`.** FastMCP 4.0 (2026-08-31) moved the
   proxy and OpenAPI modules, renamed `mount(prefix=)` → `mount(namespace=)`, and
   replaced `add_tool_transformation`. `pyproject.toml` says `>=3.0.0` with no
   upper bound, so a plain `pip install beherouter` resolves 4.x today. Every
   FastMCP name this design touches is imported in **one** module
   (`backends/inproc.py`, below) so the 4.x port is one file.
2. **Isolate attach per surface.** `build_surfaces` guards cost computation but
   not `load_backend` (`gateway.py:98-99`). New sources raise the stakes — an
   OpenAPI spec URL is network I/O at attach. A failed attach mounts a
   **degraded surface**: it answers RFC 9457 `503` naming the surface only
   (never the error: the path is reachable before auth), `/healthz` reports `{"status": "degraded", "surfaces": [...],
   "failed": ["name"]}` with HTTP 200 (the gateway is up; the monitor alerts on
   the field, not the status code), and a background task retries with backoff.
   That retry task enters **and holds** the surface's `http_app()` lifespan
   itself: the lifespan starts an anyio task group, and exiting one from a
   different task than entered it raises, so the parent's exit stack cannot
   own it.
   Every attach is bounded by `BEHEROUTER_ATTACH_TIMEOUT_S` (default 30).
   The **config** errors that `registry-lint` can see (`UsageError` from
   `validate_entry`, an unset `${VAR}`) still refuse to boot: those are
   operator mistakes with a local fix, and a gateway that boots around them
   hides them.
3. **A public plugin API.** `beherouter.plugin_api` re-exports exactly what an
   author needs — `register`, `PluginSpec`, `ConfigField`, `EnvVar`,
   `IdentitySupport`, `PluginContext`, `McpBacking`, `load_mcp_backend`,
   `UsageError`, and (Phase 1) `load_inproc_backend` — plus `API_VERSION = 1`.
   `PluginSpec` gains `api: int = 1`; `register()` refuses a spec whose `api`
   the gateway does not serve, naming both numbers. In-tree plugins keep their
   relative imports; the facade is exercised by a subprocess test that installs
   a fake distribution — see the plan's Task 4 for the circular-import reason.
   Internal paths stay importable but carry no promise.

### Phase 1 — the `inproc` backing

#### Declaration

```python
# plugins/spec.py
BACKINGS = ("native", "http", "stdio", "cli", "inproc")

# backends/backing.py
@dataclass(frozen=True)
class McpBacking:
    name: str
    transport: str  # "stdio" | "http" | "inproc"
    ...
    server: "FastMCP | None" = None   # inproc only
```

`inproc` is its own backing, not a flavour of `native`, because the identity
target follows the backing (`IdentitySupport.target`) and an in-process MCP
server's natural target is **`header`** — the headers its outbound HTTP client
sends — where `native`'s is `credential`.

#### The loader — `backends/inproc.py`

```python
async def load_inproc_backend(backing: McpBacking) -> Backend:
    # list: the SAME pipeline as every MCP backend
    async with Client(FastMCPTransport(backing.server)) as client:
        backend = await backend_from_client(backing.name, client, backing.pinned,
                                            backing.republish_output_schema, backing.notes)
    backend.kind = "inproc"
    backend.executor = InprocExecutor(backing)
    backend.relist = <same shape, fresh in-memory session>
    return backend
```

`InprocExecutor.run(verb, args, *, identity=None)`:

1. `backing.guard(verb, args)` if set — same order as `ReconnectingMCPExecutor`.
2. **Validate `args` against the tool's input schema** (`jsonschema`, the
   schema captured at list time) and raise `UsageError` naming the field on
   failure. Neither FastMCP path does this for an OpenAPI tool (see § Measured
   2026-09-26, Q1).
3. Run the call in a **blank `contextvars.Context()`**
   (`asyncio.create_task(..., context=contextvars.Context())`). Inside it, set
   `CURRENT_IDENTITY_HEADERS` (a `ContextVar[dict[str, str]]`, owned by
   `backends/inproc.py`) to `identity.headers` or `{}`. The blank context is
   what stops the gateway caller's inbound headers reaching the upstream (Q3),
   and it also makes the reset-after-exception guard hold by construction.
4. `await backing.server.call_tool(verb, args)`. Map `ToolError` **and**
   `fastmcp.exceptions.ValidationError` (not a `ToolError` subclass) to
   `UsageError`, and anything else to `Unavailable`.
5. Return `{"result": _payload(res)}`.

`backends/inproc.py` also exports the one piece a source needs to honour
identity:

```python
def identity_client(**httpx_kwargs) -> httpx.AsyncClient:
    """An httpx client that applies the current call's identity headers OVER
    its attach-time headers — per request, never mutating shared state."""
```

That is the whole identity story for `inproc`: `identity.headers` are computed
by the existing `IdentityPolicy` exactly as for `http` (modes `bearer`,
`claims`, `client`, `lookup`), and land on the upstream request instead of on an
MCP hop.

#### Guards that must hold (tests, red first)

- A per-call identity reaches the upstream request; two concurrent calls with
  different identities each see only their own (the spike, as a test).
- **No silent shared fallback**: a surface with `[surface.identity]` whose
  source did not build its client with `identity_client` is refused at attach —
  `load_inproc_backend` checks for a marker attribute `identity_client` sets.
  This is the `stdio` rule ("believed per-user, actually shared") restated for
  a new backing.
- `inproc` descriptors for a given server equal those `load_mcp_backend` yields
  for the same server over `http` (one pipeline, proven).
- `ContextVar` is reset after an exception.

### Phase 1 — three sources on the seam

#### `openapi` — a customer's REST API, no code

```toml
[crm]
plugin = "openapi"
pinned = ["get_customer", "search_customers"]
probe = "get_customer"
probe_args = { id = "1" }
  [crm.config]
  spec = "/etc/beherouter/specs/crm.yaml"   # a path; see "spec by URL" below
  base_url = "https://crm.internal"
  include = ["get_customer", "search_customers", "create_note"]   # operationIds
  auth_header = "authorization"
  auth_prefix = "Bearer "
  [crm.env]
  token = "${BEHEROUTER_CRM_TOKEN}"
```

`build()` = parse the spec, `FastMCP.from_openapi(spec, client=identity_client(
base_url=..., headers={auth_header: auth_prefix + token}), route_map_fn=...)`,
`load_inproc_backend(...)`. Declared `IdentitySupport(modes=("bearer",
"claims", "client", "lookup"), target="header")`.

Rules, each a lint-time error unless stated:

- **`include` is required.** No "every operation" default. Tool-count explosion
  and thin generated descriptions are the documented failure of OpenAPI→MCP
  (FastMCP's own docs warn that auto-converted servers underperform curated
  ones). An operator who wants everything writes `include = ["*"]` and gets a
  lint **warning** naming the count.
- **Every `include` name must be an `operationId` in the spec.** Lint reads the
  file — offline, so the "no network at lint" rule holds.
- **Tool names are `operationId`s**, validated against `^[A-Za-z0-9_-]{1,64}$`.
  An operation without one is refused by name with the fix
  (`x-mcp-name` or add an `operationId`), not given a `get_pets_id_` name.
- **`probe` is required** (see generic plugins below for why).
- **Spec by URL** (`spec = "https://..."`) is allowed and is fetched in
  `build()`, never at lint; lint then cannot check `include` and says so as a
  warning. Phase 0's per-surface isolation is what makes this safe.
- `search_aliases` and per-tool `notes` work unchanged, and matter more here:
  OpenAPI summaries are the thinnest descriptions any source produces.

#### `python-dir` — drop a file, get tools

```toml
[ops]
plugin = "python-dir"
pinned = ["restart_worker"]
probe = "ping"
  [ops.config]
  path = "/etc/beherouter/tools/ops"
```

```python
# /etc/beherouter/tools/ops/workers.py
from fastmcp.tools import tool

@tool(annotations={"readOnlyHint": False})
def restart_worker(name: str) -> str:
    """Restart one queue worker by name."""
    ...
```

`build()` = `FastMCP(providers=[FileSystemProvider(path)])` →
`load_inproc_backend`. **No identity support in this phase** — a function that
wants the caller has nowhere stable to read it from yet; exposing the
`ContextVar` as API is a later decision, and the fail-closed default (no modes)
applies.

⚠️ **This executes operator-supplied code inside the gateway process.** That is
the same trust as installing a plugin package, stated plainly in PLUGINS.md and
in the plugin's summary. `FileSystemProvider(reload=True)` is never used: a
module that fails to import is logged and skipped by FastMCP, which for us would
be a surface that attached green and serves fewer tools than the operator wrote
— so `build()` imports each module itself first and raises naming the file.

#### `native` authors get the decorator path too

`plugin_api` exposes `load_inproc_backend`, so an out-of-tree plugin that is
"some Python functions" becomes:

```python
mcp = FastMCP("acme")

@mcp.tool
def lookup_order(order_id: str) -> dict:
    """Fetch one order."""
    ...

SPEC = PluginSpec(name="acme-orders", summary="...", backing="inproc",
                  pinned=("lookup_order",), probe="lookup_order",
                  probe_args={"order_id": "probe"})

async def build(ctx):
    return await load_inproc_backend(McpBacking(name=ctx.surface,
                                                transport="inproc", server=mcp,
                                                pinned=ctx.pinned))

register(SPEC, build)
```

≈ 15 lines against today's ≈ 50–60, with schemas generated from type hints.

### Phase 1 — decisions (brainstorm, 2026-09-26)

Settled after the Q1–Q3 measurements. They override the text above where the
two differ.

1. **Two plans.**
   - **1a:** the `inproc` backing, `identity_client`, the decorator path
     (`plugin_api.load_inproc_backend`), the `openapi` source, and the e2e
     `openapi` surface with two `client`-mode callers.
   - **1b:** `python-dir`, planned once 1a has landed. It runs operator code
     in the gateway process, so it gets its own trust review.
2. **`InprocExecutor.run` order:**
   - `guard` first.
   - Then `jsonschema` validation against the schema captured at list time,
     refreshed on each TTL re-list.
   - Then the call, in a blank `contextvars.Context` with the identity var set
     inside it.
   - Errors: `ToolError` and `fastmcp.exceptions.ValidationError` map to
     `UsageError`; anything else maps to `Unavailable`.
   - `args.prepare_args` still runs before the executor, unchanged.
3. **The `openapi` source closes every generated schema.** It sets
   `additionalProperties: false` through `mcp_component_fn`. An undeclared or
   misspelled argument then gets `prepare_args`' existing did-you-mean refusal,
   and `describe_tool` shows the closed schema. There is no opt-out flag: an API
   that takes undeclared parameters is out of scope until a customer brings one.
4. **FastMCP stays in one module.**
   - `backends/inproc.py` exposes
     `openapi_server(spec, *, client, include) -> FastMCP`. It wraps
     `FastMCP.from_openapi`, the total `route_map_fn` built on
     `MCPType.EXCLUDE`, and the schema closing.
   - It then asserts that the tool names equal `include`, raising
     `UsageError` naming the difference. That assertion is the guard against
     the fail-open filter.
   - `plugins/openapi.py` imports nothing from FastMCP.
5. **Refinements from reading FastMCP 3.4.5 source while planning 1a:**
   - The `jsonschema` pre-check in decision 2 applies only to tools that are
     **not** `FunctionTool`s, and it checks the live tool from
     `server.get_tool(verb)`. A function tool validates and **coerces** with
     pydantic on both paths (`"3"` is accepted for an `int`), and a blanket
     check would refuse calls that work today.
   - The `operationId` rule is `^[A-Za-z0-9]+(_[A-Za-z0-9]+)*$`, at most 56
     characters, replacing `^[A-Za-z0-9_-]{1,64}$`. FastMCP slugifies an
     `operationId` (`-` becomes `_`, `__` splits it, names are truncated at
     56), so only an id the slug leaves unchanged is published under its own
     name.
   - `mcp_component_fn` fails open like `route_map_fn`. `openapi_server`
     (async) re-checks both the names and the closed schemas through the
     public `list_tools()`.
   - `PluginSpec.requires_entry` moves forward from Phase 2 into 1a, because
     `openapi` needs `probe` and `pinned` to be mandatory.
6. **Unchanged:** the `openapi` lint rules, the refusal when a surface
   declares identity but its source isn't identity-aware, `native` as it is, and
   no identity support for `python-dir`.

### Phase 1b — `python-dir` decisions (brainstorm, 2026-09-26)

Settled after reading FastMCP 3.4.5's `providers/filesystem.py` and
`filesystem_discovery.py`. They override § `python-dir` above where the two
differ.

1. **`FileSystemProvider` is not used.** It fails open three ways: a file that
   fails to import is logged and skipped; a missing root only warns, yielding
   zero tools; and it is built `on_duplicate="replace"`, so a second file
   defining the same tool name silently wins. `backends/inproc.py` instead
   exposes `python_dir_server(path, *, name) -> FastMCP`, which calls
   `discover_and_import(path)` itself and adds each tool to a fresh `FastMCP`.
   It raises `UsageError` on:
   - a root that is missing or not a directory;
   - any entry in `failed_files`, naming the file and the error;
   - zero tools;
   - two **distinct** functions published under one name, naming both files.
     Compared by the wrapped function (`tool.fn`), not by name and not by the
     `Tool` object: `extract_components` scans `dir(module)`, so a tool imported
     from a sibling module is reported under both files, and it builds a fresh
     `Tool` each time (measured: same `fn`, different `Tool`). That is not a
     duplicate.
   Resources, templates and prompts are dropped with a warning; the gateway
   surfaces tools only.
2. **The plugin** (`plugins/python_dir.py`, no FastMCP import): backing
   `inproc`; one config field, `path`, absolute; `requires_entry =
   ("probe", "pinned")`; no `IdentitySupport`, so `[surface.identity]` is
   refused at lint. Its summary states that it runs operator code in the
   gateway process.
3. **Lint is static and never executes operator code.** `ast.parse` each
   `.py` file under `path` (same walk as `discover_files`: recursive, skipping
   `__init__.py` and `__pycache__`). A syntax error fails, naming the file and
   line. Tool names are collected from top-level functions decorated `tool`
   (bare, called, or as an attribute such as `fastmcp.tools.tool`); the name is
   the `name=` keyword, else a positional string, else the function name; names
   starting `_` are skipped, as `extract_components` does. `pinned` and `probe`
   must be among them, through a generic `Plugin.published(config) -> set[str]
   | None` hook that `validate_entry` checks `pinned` and `probe` against; the
   `openapi`-specific `include` block in `validate_entry` moves onto the same
   hook (which also starts refusing an `openapi` probe outside `include`). A
   path that does not exist on the linting machine is a
   **warning**, not a failure. The static list is best-effort; the attach-time
   checks in (1) are authoritative.
4. **Calls** reuse the `inproc` executor unchanged, including its error
   classification.
5. **Out of scope for 1b:** hot reload; identity for directory functions; chart
   packaging of the directory (`extraVolumes` / `extraVolumeMounts` already
   cover it); an e2e surface (unit plus gateway tests suffice for a source with
   no network side).

### Phase 2 — generic `mcp-http` and `mcp-stdio`

`url`/`cmd` left `registry.toml` because `gitea-home` attached with no probe and
served a dead token for a day. The generic plugins bring the capability back
**without** the hole:

```toml
[wiki]
plugin = "mcp-http"
pinned = ["search_pages", "get_page"]
probe = "search_pages"
probe_args = { query = "a", limit = 1 }
  [wiki.config]
  url = "http://wiki-mcp:8000/mcp"
  auth_header = "authorization"   # optional
  [wiki.env]
  token = "${BEHEROUTER_WIKI_TOKEN}"   # optional
```

- **`probe` and `pinned` are required on a generic plugin** — the one place
  they stop being overrides, because there is no tested default to fall back to.
  Enforced by a new `PluginSpec.requires_entry: tuple[str, ...] = ()` that
  `validate_entry` checks (`("probe", "pinned")` for the generics and
  `openapi`), so the rule is data, not a special case.
- An **optional** credential needs `EnvVar(required=False)` — today every
  declared credential is mandatory (`registry.py`, "requires credential(s)").
- `mcp-http` declares `IdentitySupport(modes=("bearer","claims","client"),
  target="header")`; `mcp-stdio` declares none (stdio never can).
- `mcp-stdio`'s `cmd` must exist in the image — the existing
  `_require_command` and the lint warning apply unchanged.
- Maturity is always `declared` (below), and `beherouter plugins` says
  "generic" so no one mistakes `mcp-http` for a curated plugin.

### Phase 3 — `beherouter catalog import` (the `server.json` path)

The official MCP Registry's `server.json` (schema `2025-12-11`) says how to
**reach or launch** a server and which secrets it needs — `remotes[]` (URL,
headers) and `packages[]` (`registryType` npm/pypi/oci/…, `transport`,
`environmentVariables` with `isSecret`/`isRequired`). It carries **no tool list,
no pins and no probe**, so it is an import *source*, not a plugin.

```
beherouter catalog import <file | URL | registry-name> --surface wiki [--registry URL]
```

emits, like `plugin-config`, fragments — never a credential:

- `remotes[0]` with `streamable-http` → an `mcp-http` entry; each declared
  header becomes an `EnvVar` placeholder.
- `packages[]` with `pypi` / `npm` and `stdio` transport → an `mcp-stdio` entry
  with `cmd = "uvx <id>==<version>"` / `"npx -y <id>@<version>"` — **pinned to
  the exact version**, never `latest` — plus a warning that the image must ship
  the runtime (`npx` is not in the published image).
- `pinned` and `probe` are emitted **as TODO placeholders that fail lint**, so an
  imported entry cannot be committed until someone chose them. That is the
  `gitea-home` rule surviving the import path.

**Our own metadata rides in `server.json`, not beside it.** The registry reserves
`_meta."io.modelcontextprotocol.registry/publisher-provided"` for publisher
data. A server that wants a first-class beherouter experience publishes:

```json
"_meta": {
  "io.modelcontextprotocol.registry/publisher-provided": {
    "io.beherouter/plugin": {
      "v": 1,
      "pinned": ["search_pages", "get_page"],
      "probe": "search_pages",
      "probe_args": {"query": "a", "limit": 1},
      "search_aliases": {"search_pages": ["wiki", "docs"]},
      "identity": {"modes": ["bearer"], "target": "header"}
    }
  }
}
```

and import fills those in instead of TODOs. The block is **advisory**: it
becomes registry-entry overrides an operator reviews, never trusted spec data,
and `identity` modes are only honoured if the target plugin (`mcp-http`)
already declares them.

`beherouter catalog export <plugin>` writes the reverse for our curated
plugins, so `office-mcp`, `sonarqube` and `plane` can be listed in a private
subregistry with their pins and probes intact.

Out of this phase: running a subregistry, MCPB bundles (a desktop install
format; a stdio `cmd` covers the gateway's need).

### Phase 3 — maturity tiers

A tier is a claim about **evidence**, and every tier above the first is checked
by a test, never merely declared:

| Tier | Means | Checked by |
|---|---|---|
| `declared` | a spec exists | always true; every generic and imported entry |
| `probed` | has a probe that is a credentialed `tools/call` with valid arguments | conformance test: `probe` set, `probe_args` validate against the tool's schema in the recorded catalogue |
| `catalogued` | pins proven against a recorded catalogue | recorded catalogue in `tests/search_eval/catalogues/` or `tests/fixtures/catalogues/`; every pin and every `search_aliases` key is in it |
| `verified` | attached for real end to end | an e2e test id recorded on the spec (`evidence=("tests/e2e/test_x.py::test_y",)`) that exists in the tree |
| `per-user` | identity proven end to end | `verified` **and** declared `IdentitySupport` **and** an e2e test asserting `matches_caller` |

```python
PluginSpec(..., maturity="verified", evidence=("tests/e2e/test_plane.py::test_pat_alice",))
```

- One parametrized test over `PLUGINS` checks every in-tree plugin meets the
  tier it declares; the same checks ship as `beherouter.testing.
  plugin_conformance(spec)` for out-of-tree authors (the Singer-SDK pattern).
- A tier may only be **declared**, never computed at runtime — the gateway does
  not run pytest. What the gateway can check at runtime is already reported by
  `health --deep`; the tier is the offline half.
- `beherouter plugins` prints the tier; `registry-lint` warns (never fails) on an
  entry whose plugin is `declared`, naming what would raise it.
- Out-of-tree plugins cannot claim `verified` or above unless their `evidence`
  paths resolve in their own distribution; otherwise they are shown as
  `probed` at most, with a note.

Expected initial tiers: `office-mcp` verified · `plane` verified ·
`plane-http-apikey` per-user · `plane-http` per-user · `sonarqube` probed ·
`gcal`/`m365` catalogued (never attached) · `openapi`/`python-dir`/`mcp-*`
declared.

## Testing

Red first, per phase:

- Phase 0: `fastmcp` upper bound present; an attach raising `Unavailable` or
  timing out leaves the other surfaces serving and `/healthz` reporting
  `degraded`; a `validate_entry` error still refuses boot; `register()` refuses
  a mismatched `api`; an external plugin importing only from `plugin_api`
  registers even when the facade is the process's first import.
- Phase 1: the spike as tests (identity per call, concurrency, reset on error);
  descriptor parity `inproc` vs `http` for one server; an invalid argument
  refused before the upstream and a FastMCP `ValidationError` surfacing as
  `UsageError` (Q1); a caller's inbound header absent upstream (Q3); an
  `include` filter yielding exactly `include` (Q2); the "identity declared
  but source not identity-aware" refusal; `openapi` lint cases (missing
  `include`, unknown operationId, missing operationId, bad name, `["*"]`
  warning); `python-dir` import error names the file.
- Phase 2: `requires_entry` enforced; optional `EnvVar`; `mcp-stdio` refuses
  identity at lint.
- Phase 3: import of a recorded `server.json` for each shape (remote, pypi,
  npm, with and without the `_meta` block) → lint fails until TODOs are
  replaced; export → import round-trips a curated plugin's pins and probe;
  every in-tree plugin satisfies its declared tier.
- e2e: one `openapi` surface against a stub REST service in `tests/e2e/`, with a
  `client`-mode identity proving two callers reach the upstream as themselves.

## Docs

`docs/PLUGINS.md` gains § Sources (openapi, python-dir, generic) and §
Maturity; the `native` section points at `inproc` for "some Python functions".
AGENTS.md § Plugins: five backings, the generic plugins' probe rule, the
python-dir trust statement. README catalogue: the tier column.

## Out of scope

- A declarative REST YAML without OpenAPI (Unla/Airbyte shape). Revisit if
  customers bring APIs with no spec.
- Hot reload of `python-dir` or of the registry — the published tools array is
  frozen per process by design.
- Per-surface tool overrides (description, annotations, argument hide/rename
  via `ToolTransformConfig`). Valuable, especially for thin OpenAPI
  descriptions, and orthogonal — its own design.
- A scaffold command and copier template (`beherouter plugin new`). Easier
  after Phase 0's `plugin_api` exists; its own small design.
- FastMCP 4.x migration itself.
- Moving `gcal`/`m365` onto `inproc`.

## Open questions the implementer must settle by measurement

1. ~~Does `server.call_tool(..., run_middleware=True)` validate arguments
   against the input schema the same way a `tools/call` over a session does?~~
   **Settled (2026-09-26): the two paths behave the same, and for an OpenAPI
   tool neither one validates.** The direct path must validate first. See §
   Measured 2026-09-26, Q1.
2. ~~`FastMCP.from_openapi` with `route_map_fn` vs. building an
   `OpenAPIProvider` directly?~~ **Settled (2026-09-26): `from_openapi` +
   `route_map_fn`.** They are the same code, but `route_map_fn` fails open. See
   § Measured 2026-09-26, Q2.

### Measured 2026-09-26 (spikes, FastMCP 3.4.5)

The spike scripts built a four-operation OpenAPI 3.0 spec, served it with
`FastMCP.from_openapi` over an `httpx.MockTransport` that records every
upstream request, and used a decorated function tool as the control.

**Q1: argument validation.**

| Case | OpenAPI tool, direct | OpenAPI tool, session | Function tool, both paths |
|---|---|---|---|
| wrong type (`id: "not-an-int"`) | **reaches upstream** as `/customers/not-an-int` | same | refused (pydantic) |
| missing required path param | **reaches upstream** as literal `/customers/{id}` | same | refused |
| missing required query / body field | reaches upstream | same | refused |
| violates `maximum` | reaches upstream | same | — |
| extra argument | reaches upstream, dropped | same | refused |
| `"3"` for an `int` | — | — | coerced, both paths |

- The session path adds no validation of its own: FastMCP's `tools/call`
  handler doesn't use the MCP SDK's `validate_input`. So the premise "a call
  the MCP path would refuse" doesn't hold. Instead, **nothing refuses an
  invalid call to an OpenAPI tool.** `OpenAPITool.run` builds whatever request
  it can.
- The generated input schemas are accurate: the right types, `required` and
  `maximum`. So `jsonschema.validate(args, tool.parameters)` refuses every
  invalid case above, with nothing reaching the upstream. The one exception is
  an extra argument: the schema has no `additionalProperties: false`, and the
  upstream drops the argument. Whether to tighten a copy of the schema is a plan
  decision. `jsonschema` 4.26 is already in the lock through `mcp`. Declare it
  directly if it is imported.
- **What the gateway already covers.** `args.prepare_args` runs on every
  pinned and `run_tool` call for every backing, before the executor. It refuses
  a missing required argument, and it refuses an undeclared one when the schema
  is **closed** (`additionalProperties: false`). So the gap in the executor is
  **types and constraints**, plus undeclared arguments on the *open* schemas
  that OpenAPI tools produce.
- On the direct path, a function tool's bad call raises
  `fastmcp.exceptions.ValidationError`, and the session path wraps it as
  `ToolError`. `ValidationError` is **not** a `ToolError` subclass (both derive
  from `FastMCPError`), so an executor that maps only `ToolError` turns a caller
  mistake into a transient `Unavailable`.

**Q2: the include-list.**

- `FastMCP.from_openapi(spec, client=..., route_map_fn=...)` is a ten-line
  wrapper that builds `OpenAPIProvider` with the same arguments and returns
  `FastMCP(providers=[provider])`. Both produced the same tool set. Use
  `from_openapi`: it is the documented entry point, and the module path of
  `OpenAPIProvider` is one of the things FastMCP 4.0 moved.
- The filter is
  `route_map_fn = lambda route, t: t if route.operation_id in include else MCPType.EXCLUDE`.
  `MCPType` is the only other name imported, and it stays in
  `backends/inproc.py` (or `plugins/openapi.py`, if the plan puts it there).
- ⚠️ **`route_map_fn` fails open.** An exception inside it is logged at
  WARNING and the route takes the **default** mapping, which is TOOL. A raising
  filter therefore publishes every operation. The filter must be total, with no
  lookups that can raise, and a test must assert the resulting tool set equals
  `include`. Checking `set(tool names) == include` after construction is the
  cheap belt-and-braces guard.
- An operation without an `operationId` gets a generated name
  (`POST_customers`). This confirms the spec's rule to refuse such an operation
  by name at lint.

**Q3 (found while measuring Q1): the gateway caller's headers leak upstream.**

`OpenAPITool.run` merges `fastmcp.server.dependencies.get_http_headers()` into
every upstream request, for each header the request lacks. That function reads
the **current inbound MCP HTTP request**. It excludes `authorization`,
`accept`, `host` and similar, but nothing else. The test was a direct
`inner.call_tool` from inside an outer FastMCP HTTP request, the gateway's
shape. The caller sent `x-api-key`, `x-workspace-slug` and `cookie`, and the
upstream received all three, plus `mcp-protocol-version`. On an
identity-bearing gateway, that forwards one backend's per-user credential to
every OpenAPI surface.

- **Fix, measured:** run the inner call in a blank `contextvars.Context()` and
  set the identity var inside it. The upstream then saw no caller header. It
  still saw the per-call identity (`Bearer alice`) or the deployment default
  (`Bearer deploy`). Three concurrent isolated calls each saw only their own
  identity.
- This has to be a red-first test in Phase 1: a caller header present inbound
  must be absent upstream.
3. ~~The degraded-surface retry: fixed backoff, or only on the next TTL re-list?~~
   **Settled (Phase 0):** exponential backoff, 5 s doubling to 300 s; a retried
   attach that succeeds is swapped in once, publishing the pinned set and
   freezing it, as a first attach would.

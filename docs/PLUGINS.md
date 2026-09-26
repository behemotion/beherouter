# Writing a plugin

A plugin is the **only** way to attach a backend to beherouter. There is no `kind`, `url`,
`cmd` or `transport` in `registry.toml`; an entry is a plugin name plus overrides.

The goal is that attaching a service is *a name in a config file*, not an act of
archaeology — so everything hard-won about a backend (which tools it really serves, which
credential it needs, which of its quirks will bite you) lives in the plugin, versioned with
the code it explains.

## The contract

Two halves, and the split is load-bearing:

```python
from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from . import register
from .spec import ConfigField, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="office-mcp",
    summary="Agent file toolbox: convert, inspect and edit documents, images, audio and video.",
    backing="http",
    pinned=("discover", "invoke", "file_from_url", "job_status"),
    probe="discover",
    probe_args={"query": "pdf"},
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default="http://office-mcp:8100/mcp/",
            doc="MCP endpoint on the shared behe-gateway network.",
        ),
    ),
)


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["base_url"],
            pinned=ctx.pinned,
        )
    )


register(SPEC, build)
```

That is a complete, production plugin (`src/beherouter/plugins/office_mcp.py`).

- **`SPEC`** — a frozen `PluginSpec` dataclass. Pure data. No behaviour, no I/O.
- **`build(ctx)`** — one `async` function returning a `Backend`.
- **`register(spec, build, validate=None)`** — adds it to the registry. A duplicate name is
  a programming error and raises.

## Why the spec must stay inert

Three commands read a plugin **without attaching anything**: `plugins`, `plugin-config` and
`registry-lint`. That is only possible because the declaration is data.

It also makes *"attach performs no network I/O"* **mechanically enforceable** rather than a
convention someone has to remember. A plugin structurally **cannot** phone home from its
spec — only from `build`. The calendar plugins hold this with a test,
`test_attach_performs_no_network_io` (`tests/test_plugin_m365.py`), and the property it
buys is concrete: a revoked OAuth grant fails a *probe* instead of crash-looping the
gateway.

If you find yourself wanting to do I/O to compute a spec field, that is the design telling
you the value belongs in `config` or in `build`.

## The five backings

| Backing | What it is | Choose it when |
|---|---|---|
| `http` | An MCP server reached over HTTP | The backend already runs as a service |
| `stdio` | An MCP server run as a subprocess | You would otherwise add a container for it — a subprocess costs a process instead. Also sidesteps remote transports that 401 an un-credentialed probe, which some clients misread as "this server wants OAuth" |
| `cli` | A beheaxi CLI, described and invoked as tools | The backend is a command-line tool following the harness CLI contract |
| `native` | In-process Python | No sidecar, no extra runtime, no writable state — the calendar plugins are these |
| `inproc` | An in-process **FastMCP server** the plugin builds, listed through the same MCP pipeline as `http`/`stdio` and called directly (`server.call_tool`) | You have "some decorated Python functions", or a REST API with an OpenAPI document (the `openapi` source below). Identity target `header`, through `identity_client` |

Three things `inproc` does that the other MCP backings do not have to:

- **It validates arguments itself, for tools that aren't functions.** Nothing in
  FastMCP 3.4.5 validates an OpenAPI tool's arguments: a wrong type, a missing
  field or a missing path parameter goes out to the REST API, the last as a
  literal `/customers/{id}`. The executor checks such a tool against its schema
  with `jsonschema` first. Function tools are left to pydantic, which also
  **coerces**: `"3"` is accepted for an `int`, exactly as over a session.
- **It runs each call in a blank `contextvars.Context`** holding only the
  per-call identity. FastMCP's OpenAPI tool copies the headers of the *current
  inbound request* onto its upstream request. Run in the gateway's own request
  context, that forwarded the gateway caller's `x-api-key` and `cookie` to the
  REST API.
- **It classifies a failure by its cause, not its type.** `call_tool` re-raises
  any exception as a `ToolError`. A tool's own `ToolError`, a validation error or
  an upstream 4xx is the caller's to correct (`UsageError`). A crash, an
  upstream 5xx or a network failure is an outage (`Unavailable`).

## Sources: `openapi`

A customer's REST API, attached from its OpenAPI document with no MCP server and
no plugin code:

```toml
[crm]
plugin = "openapi"
pinned = ["get_customer"]            # required: there is no tested default
probe = "get_customer"               # required, and must be a credentialed call
probe_args = {id = 1}
  [crm.config]
  spec = "/data/crm-openapi.yaml"    # a local .json/.yaml, or an http(s) URL
  base_url = "https://crm.internal"
  include = ["get_customer", "search_customers"]
  [crm.env]
  api_key = "${BEHEROUTER_CRM_API_KEY}"
  [crm.identity]                     # optional: per-user, like any surface
  mode = "client"
    [crm.identity.map]
    authorization = "x-crm-token"
```

`auth_header` (default `authorization`) and `auth_prefix` (default `"Bearer "`)
say where the deployment credential goes. The rules, all checked offline by
`registry-lint` unless stated:

- **`include` is required.** It lists the `operationId`s to publish. `["*"]`
  publishes every operation, and lint warns with the count: auto-converted
  catalogues are the documented failure of OpenAPI→MCP, so list what agents
  need.
- **Each included `operationId` must exist**, and a near miss is refused with a
  did-you-mean. It must also be a name FastMCP's slug leaves **unchanged**
  (`^[A-Za-z0-9]+(_[A-Za-z0-9]+)*$`, at most 56 characters). Otherwise the tool
  would silently be published under another name, and lint names that name. An
  operation with no `operationId` is refused under `["*"]` by method and path.
- **`probe` and `pinned` are required** (`requires_entry`, below). Every pin must
  be in `include`.
- **`spec` may be a URL.** It is fetched in `build()`, never by lint, so lint
  warns that it cannot check `include` against it. Use a local file to catch a
  bad name before a deploy.
- **Every schema is closed** (`additionalProperties: false`, no opt-out), so a
  misspelled argument is refused with a did-you-mean instead of being dropped by
  the upstream.

Both FastMCP hooks this uses (`route_map_fn`, `mcp_component_fn`) **fail
open**: an exception in one is logged and ignored, and a failing filter would
publish every operation. So the built server is checked afterwards, through
the public `list_tools()`: the published names must equal `include`, and every
schema must be closed.

⚠️ OpenAPI summaries are usually thin, so `search_aliases` and `notes` matter
more here than for a hand-written MCP server. Check what `search_tools` finds
before you rely on it.

## The decorator path

"Some decorated functions" is a whole plugin:

```python
from fastmcp import FastMCP
from beherouter.plugin_api import McpBacking, PluginSpec, load_inproc_backend, register

mcp = FastMCP("acme")

@mcp.tool
def lookup_order(order_id: str) -> dict:
    """Fetch one order."""
    return {"order_id": order_id}

SPEC = PluginSpec(name="acme-orders", summary="orders", backing="inproc",
                  pinned=("lookup_order",), probe="lookup_order",
                  probe_args={"order_id": "probe"})

async def build(ctx):
    return await load_inproc_backend(McpBacking(name=ctx.surface, transport="inproc",
                                                server=mcp, pinned=ctx.pinned))

register(SPEC, build)
```

Ship it as an out-of-tree plugin (below) and name it in `registry.toml`. Two
things to know:

- ⚠️ **A decorated server can't be configured per-user yet.** Per-user needs
  two things. Its outbound HTTP client must come from `identity_client(...)`, an
  `httpx.AsyncClient` that puts the caller's identity headers over its own, per
  request. The server must also be marked identity-aware. `openapi_server` does
  both for the `openapi` source, but `plugin_api` has no public way to mark a
  server yet. A surface that declares `[surface.identity]` on an unmarked
  source is **refused at attach**, never served as the deployment: the stdio
  rule, restated for `inproc`.
- Pre-validation applies to **non-function tools only**. Function tools validate
  and coerce with pydantic, as they would over a session.

## `PluginSpec`, field by field

```python
@dataclass(frozen=True)
class PluginSpec:
    name: str
    summary: str
    backing: str                       # one of BACKINGS
    pinned: tuple[str, ...] = ()
    probe: str | None = None
    probe_args: dict | None = None
    config: tuple[ConfigField, ...] = ()
    env: tuple[EnvVar, ...] = ()
    catalogue_ttl_ms: int = 300_000    # 0 disables refresh
    search_aliases: Mapping[str, tuple[str, ...]] = {}  # tool -> extra search words
    requires_entry: tuple[str, ...] = ()  # entry keys with no tested default
```

| Field | Meaning |
|---|---|
| `name` | The name used in `registry.toml` as `plugin = "<name>"` |
| `summary` | One line, shown by `beherouter plugins` |
| `backing` | `"http"`, `"stdio"`, `"cli"`, `"native"` or `"inproc"` |
| `pinned` | The tools published in the surface's `tools` array |
| `probe` / `probe_args` | The tool `health --deep` calls to prove the backend really works |
| `config` | The keys allowed in `[surface.config]` |
| `env` | Credentials, by **logical** name |
| `catalogue_ttl_ms` | How long the searchable catalogue stays fresh |
| `search_aliases` | Extra search words per tool: what agents type that the backend's descriptions lack. See § Search vocabulary |
| `requires_entry` | Registry-entry keys (`"probe"`, `"pinned"`) the entry **must** set, for a generic source with no tested default to fall back to. `validate_entry` refuses an entry without them, and `plugin-config` emits them |

## `pinned` and `probe` are overrides, not required knowledge

⚠️ This is the single most important thing to understand about the design.

A registry entry *may* override `pinned` and `probe`, but it does not have to. **Forgetting
them yields the plugin's tested behaviour rather than an unprobed surface.**

That inversion exists because of a real incident. A Gitea surface was attached with no
probe; its personal access token was later revoked; and the surface went on **listing and
searching its catalogue perfectly** while every actual tool call failed. Nothing reported
unhealthy, because nothing was making a real call. A probe is a *credentialed
`tools/call`*, not a catalogue listing — which is why `plane`'s probe is
`member(action="me")`: it authenticates with the real token and takes no other argument.

`probe` and `probe_args` **resolve as one unit.** An entry that names its own `probe`
supplies its own `probe_args`, so an override can never inherit another tool's arguments.
Note also that `probe_args` exists at all because none of office-mcp's four tools takes zero
arguments — a bare probe name could not authenticate.

The one exception is a **generic source** such as `openapi`: it has no tested
default because it knows nothing about the API until it's configured. It
declares `requires_entry = ("probe", "pinned")`, and an entry without them is
refused at lint rather than attached unprobed.

Pin resolution has exactly **one** implementation, `resolve_pinned(entry, plugin)`, for the
same reason: two call sites deciding the same thing separately is how one gets enriched and
they diverge.

## A backend whose replies break its own `outputSchema`

A pinned MCP tool is republished with the backend's `outputSchema` (wrapped in the
`{"result": ...}` envelope), and the surface **validates every reply against it**. That is
correct for an honest backend and fatal for one whose schema is wrong: sonarqube-mcp
1.27.0.4335 types fields as plain `string`/`boolean`/`object` and answers `null` for them,
so four of its five pins failed every call with `None is not of type 'string'` while a
direct call to the backend succeeded.

`McpBacking(republish_output_schema=False)` drops the schema for that backend only — the
tool is still pinned, listed and called; a code-mode host just loses the typed result. It
is per plugin, never global, so no other surface's declared shape changes. The TTL
re-list honours it too. Set it back to `True` once the backend's schemas match its replies.

## A tool the deployment serves only in part: `guard` and `notes`

A pinned tool can be served while some of its argument shapes are not. Plane
Community Edition serves `workitem`, but 404s its workspace-wide `list` and
400s any `pql`, and a model reads those errors as transient and retries. Two
`McpBacking` fields handle it:

- **`guard(verb, args)`** runs before any transport is built. It raises a
  `UsageError` for a call this deployment is known not to serve, with a
  sentence naming the fix and saying the error is not transient. It costs no
  backend round trip and does not depend on the backend being up.
- **`notes={tool: sentence}`** is appended to that tool's description at attach
  **and** on every re-list, so the published array and the searchable
  catalogue say the same thing before the model calls.

The Plane plugins set both through `edition = "community"` (the default);
`edition = "commercial"` turns them off. Refuse only failures you have
measured: a guard that refuses a working call is worse than the 404 it
replaces.

## `ConfigField` and `EnvVar`

```python
ConfigField(name="workspace_slug", type=str, required=True, doc="Workspace slug, e.g. 'acme'.")
EnvVar(name="api_key", doc="Personal access token; handed to the server as PLANE_API_KEY.")
```

`validate_config` (in `plugins/validate.py`) enforces these offline, with no plugin
instantiation: unknown keys, missing required keys and wrong types all raise `UsageError`
before anything attaches. It special-cases `bool`, because `bool` subclasses `int` in Python
and accepting `True` for an int field would silently turn a typo into the value `1`.

**`EnvVar` names a credential logically.** The plugin maps it to wherever it actually goes —
a subprocess environment variable, an HTTP header, an OAuth exchange — so the operator never
needs to know the backend's own variable name.

## `validate()` — enforcing a backend's quirks at config time

An optional third argument to `register`. Use it to turn operator knowledge into a config
error instead of a comment nobody reads.

The real example, from `plugins/plane.py`:

```python
def validate(config: dict) -> None:
    """Reject a base_url whose HOST contains an underscore."""
    base_url = config.get("base_url")
    if not base_url:
        return
    host = urlparse(base_url).hostname or ""
    if "_" in host:
        raise UsageError(
            f"plane: base_url host '{host}' contains an underscore; Django "
            f"rejects such a Host header with a bare 400 before ALLOWED_HOSTS "
            f"is consulted. Use the network alias, not the container name."
        )


register(SPEC, build, validate=validate)
```

Why it earns its place: Django's `host_validation_re` permits only `[a-z0-9.-]` plus an
optional port, and rejects a bad `Host` with a bare **400 before `ALLOWED_HOSTS` is even
consulted**. podman-compose names the container `plane_api_1`, so *the obvious value is the
broken one*. Without the validator this is a 400 with no explanation; with it, it is a
config error that names the rule.

## Catalogue freshness vs. the published tool list

These are two different things, and conflating them is the main way to misunderstand the
gateway.

- **The searchable catalogue refreshes on a TTL** (`catalogue_ttl_ms`, overridable per
  entry). `search_tools` / `describe_tool` / `run_tool` / `context_cost` therefore see a
  backend's *current* tools. A failed or slow re-list degrades to serving the last-good
  catalogue and reports status `stale` rather than failing.
- **The published `tools` array is frozen.** It is captured **once, at attach, from the
  pinned set only**, and never changes for the process lifetime.

The freeze is the design's whole point, not an incidental detail: a host's prompt cache is
never invalidated by a catalogue refresh.

## Search vocabulary

`search_tools` is lexical (no embeddings), so it can only find a tool by words
the index contains. A backend names things its own way: Plane says `cycle`,
agents say "sprint", and no amount of scoring bridges that. `search_aliases`
does:

```python
SEARCH_ALIASES = {
    "cycle": ("sprint", "iteration"),
    "workitem": ("issue", "ticket", "task", "epic", "bug", "story"),
}
SPEC = PluginSpec(..., search_aliases=SEARCH_ALIASES)
```

**The rule for adding a word:** it is a word an agent would type that the
tool's own description does *not* already contain. A word already in the
description gains nothing, and a word that belongs to another tool steals that
tool's queries. Adding "issue" to Plane's `workitem_comment` fixed "add a comment
to an issue" and broke "list issues", because "issue" already means `workitem`.
Aliases weigh ×2 in the index, the same as the description's first sentence.

**A registry entry may add words, never remove them:**

```toml
[plane.search_aliases]
cycle = ["sprint", "PI"]   # UNION with the plugin's words, never replaces
```

Additive for the same reason `pinned`/`probe` are overrides of a tested
default: an operator adding one word cannot lose the tested vocabulary.
`registry-lint` warns about a word for a tool the plugin neither pins nor has
vocabulary for (lint does not attach, so it cannot see the full catalogue), and
`health --deep` reports `aliases_unknown` for a word naming a tool the backend
does not serve. Both are warnings, never failures.

**Measure, don't guess.** A plugin with a vocabulary should have an evaluation
set: a recorded catalogue in `tests/search_eval/catalogues/` and queries in
`tests/search_eval/queries/`, gated by `tests/test_search_eval.py`. After a
backend upgrade, re-record the snapshot and re-run the gates:

```bash
uv run python scripts/record_catalogue.py plane-mcp-server==0.3.2 \
  tests/search_eval/catalogues/plane-0.3.2.json \
  PLANE_API_KEY=x PLANE_WORKSPACE_SLUG=w PLANE_BASE_URL=http://127.0.0.1:9
uv run pytest tests/test_search_eval.py -v
```

The recording runs the server over stdio with dummy credentials, which is
enough to *list* tools and never enough to call one.

## Testing a plugin

Follow `tests/test_plugin_office.py` and `tests/test_plugin_plane.py`. The rule those encode
matters more than their shape:

> **Pin lists are asserted against reality.**

`plane`'s tests assert that five tools 404 on Community Edition (`page`, `work_log`,
`milestone`, `workitem_type`, `initiative`) and that `get_pql_reference` is uncallable
upstream — as *tests*, not comments. Both were discovered on the day the surface shipped,
and both listed cleanly first.

## Attaching it

A registry entry is a plugin name plus overrides:

```toml
[office]
plugin = "office-mcp"

[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "acme"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_API_KEY}"
```

Do not hand-write the plumbing. One command emits every fragment **from the plugin's own
spec**, so they cannot disagree:

```bash
beherouter plugin-config plane plane
```

It returns three fragments — the `registry.toml` block, the reverse-proxy `not` clause, and
the env line. It **never emits a credential**, only a placeholder. Then validate offline:

```bash
beherouter registry-lint --path registry.toml
```

## Four warnings

⚠️ **A surface that fails to attach is isolated, not fatal.** A `build()` that raises, or
an attach that takes longer than `BEHEROUTER_ATTACH_TIMEOUT_S` (default 30 s), leaves that
one surface answering RFC 9457 `503` while the gateway retries it in the background (5 s,
doubling to 300 s) and swaps it in on the first success. `/healthz` stays HTTP 200 but
reports `"status": "degraded"` and lists the surface under `failed`; the error itself is
only in the container logs. What `registry-lint` can see — an unknown plugin, a bad config
value, an unset `${VAR}` — **still refuses boot**, so run it before a deploy.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty `${VAR}` is
a `UsageError` raised during `build_surfaces` — i.e. at *startup* — so it does not yield a
broken surface, it yields a **dead gateway**, `/healthz` included. Entry and secret ship
together, or not at all.

⚠️ **Probe before pinning, and re-probe the whole list after an upgrade or an edition
change.** A backend's catalogue advertises the **commercial** surface, so "the tool exists"
says nothing about whether *this* deployment serves it. Since 2026-09-10 `health --deep`
fails a surface whose pin list names a tool the backend no longer serves (`catalogue:
"pinned_missing"`), so the *existence* half is mechanical — proving a served tool still
**works** is what `probe` is for.

⚠️ **One backend credential means one identity — unless the surface declares
otherwise.** Every write through a surface with no `[surface.identity]` table is
attributed to the single account whose token or consent it carries, and making that
surface available to every user did not make it per-user.

A plugin can opt into per-user calls by declaring `IdentitySupport` on its spec:
which of the four modes it can carry, which slot they land in, and which target
names it accepts.

| `target` | For backing | The entry's map keys are | `accepts` |
|---|---|---|---|
| `header` | `http` | HTTP header names | optional (the backend's vocabulary is open) |
| `env` | `cli` | environment variable names | optional |
| `credential` | `native` | the plugin's **own** declared `env` names | **required** |
| — | `stdio` | — | cannot: a subprocess environment is fixed at spawn |

The default is **no modes**, and that is the fail-closed half: a plugin nobody has
audited for per-user use cannot be configured for it, and `registry-lint` says so
offline rather than a deploy saying it at 3am. A `native` plugin additionally needs
a provider factory — one construction path serving both the deployment provider and
every per-user one, so the two cannot drift.

Full mechanism, modes and operating notes: [`IDENTITY.md`](IDENTITY.md).

## Out-of-tree plugins

**Supported.** A distribution advertises the `beherouter.plugins` entry-point
group; the gateway imports each one at startup, after the in-tree plugins, and
the module registers exactly as an in-tree one does:

```toml
# your package's pyproject.toml
[project.entry-points."beherouter.plugins"]
acme-crm = "acme_beherouter.crm"      # a MODULE that calls register() on import
```

```python
# acme_beherouter/crm.py
from beherouter.plugin_api import ConfigField, McpBacking, PluginSpec, load_mcp_backend, register

SPEC = PluginSpec(
    name="acme-crm",
    summary="...",
    backing="http",
    pinned=(...),
    config=(ConfigField("base_url", str, required=True),),
)

async def build(ctx):
    return await load_mcp_backend(
        McpBacking(name=ctx.surface, transport="http",
                   url=ctx.config["base_url"], pinned=ctx.pinned)
    )

register(SPEC, build)
```

Nothing else changes: `PluginSpec` and `build()` are identical either way, so a
plugin can move into this tree or out of it without an edit. `beherouter
plugins`, `plugin-config` and `registry-lint` see it like any other.

Three rules worth knowing:

- ⚠️ **An entry point that fails to import is logged and skipped**, not fatal.
  One broken third-party dependency must not cost the gateway every other
  plugin. Nothing is served by a plugin that did not load: a registry naming it
  fails with `unknown plugin`, from `registry-lint`, before a deploy.
- ⚠️ **An external plugin cannot shadow an in-tree one.** Discovery runs after
  the in-tree imports and `register()` refuses a duplicate name, so the in-tree
  plugin survives the attempt.
- **A module that registers nothing is not reported as loaded** — saying
  otherwise would advertise a plugin `get()` cannot find.

Install the distribution into the same environment as the gateway. A `--with`
on a stdio surface's `cmd` does **not** do that: it reaches only that child
process. Two ways:

- **A derived image** that `pip install`s it. This is the sturdiest option,
  with no install at pod start.
- **On Kubernetes, the chart's `plugins.install`.** An init container runs
  `python -m beherouter.plugininstall` into an emptyDir on `PYTHONPATH`, for the
  gateway and the registry-lint hook alike. ⚠️ Do not do this with a bare
  `uv pip install --target`. That resolves against an empty directory and
  installs fresh copies of `httpx`, `fastmcp` and the rest, and `PYTHONPATH`
  puts them *ahead of* the gateway's own. The installer pins every shared
  dependency to the gateway's version, which refuses a conflicting plugin, and
  then prunes the duplicates. A plugin may declare `beherouter` itself as a
  dependency; the installer drops it rather than asking an index for it.

### The plugin API and its version

Import **only** from `beherouter.plugin_api`. It is the one import path that
carries a compatibility promise; `beherouter.plugins`, `beherouter.backends.*`
and the rest stay importable and promise nothing. It exports exactly:

`API_VERSION`, `AuthError`, `Backend`, `CliBacking`, `ConfigField`, `EnvVar`,
`IdentitySupport`, `McpBacking`, `PluginContext`, `PluginSpec`,
`ToolDescriptor`, `Unavailable`, `UsageError`, `identity_client`,
`load_cli_backend`, `load_inproc_backend`, `load_mcp_backend`, `register`.

`PluginSpec.api` defaults to the current `API_VERSION` (**1**), so a plugin
states nothing to be current. The number moves only on a change an existing
plugin cannot survive; additive fields keep it. `register()` refuses a plugin
written for a version this gateway does not serve, naming both versions. An
entry-point plugin refused that way is logged and skipped like any other that
fails to import, so a registry naming it then fails `registry-lint` as an
`unknown plugin`.

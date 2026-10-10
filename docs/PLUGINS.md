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
- **`register(spec, build, validate=None, warn=None, published=None)`** — adds it to the
  registry. A duplicate name is a programming error and raises. `validate` refuses a bad
  config (below); `warn` returns legal-but-a-trap findings for `registry-lint`; `published`
  returns the tool names this config publishes, computed offline, or `None` when unknowable
  at lint — `validate_entry` refuses a `pinned` or `probe` outside it.

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
| `inproc` | An in-process **FastMCP server** the plugin builds, listed through the same MCP pipeline as `http`/`stdio` and called directly (`server.call_tool`) | You have "some decorated Python functions" (a decorator plugin, or the `python-dir` source below), or a REST API with an OpenAPI document (the `openapi` source below). Identity target `header`, through `identity_client` + `mark_identity_aware` |

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

## Generic plugins: `mcp-http`, `mcp-stdio`, `beheaxi-cli`

When a backend has no curated plugin, three generic ones attach it by address.
`beherouter plugins` marks them `"generic": true` (as it does `openapi` and
`python-dir`): no tested default stands behind them, so **the entry supplies the
check that proves the surface works.** `url`/`cmd` left `registry.toml` because
`gitea-home` attached with no probe and served a revoked token for a day; these
plugins bring the address back without that hole.

| Plugin | Backing | Entry must set | Credential | Identity |
|---|---|---|---|---|
| `mcp-http` | `http` | `probe`, `pinned` | optional `api_key` → `<auth_header>: <auth_prefix><key>` (default `authorization: Bearer …`) | `bearer`, `claims`, `client`, `exchange` |
| `mcp-stdio` | `stdio` | `probe`, `pinned` | optional `api_key` → the subprocess variable named by `api_key_env` | none — stdio never can |
| `beheaxi-cli` | `cli` | `probe` | none | `claims`, as subprocess environment |

```toml
[behemem]
plugin = "mcp-http"
pinned = ["memory_search_notes", "memory_read_note", "memory_write_note"]
probe = "memory_list_directory"
  [behemem.config]
  url = "https://behemem-mcp.example.com/mcp"   # another host: reached by its vhost
  [behemem.env]
  api_key = "${BEHEROUTER_BEHEMEM_API_KEY}"

[behecheck]
plugin = "mcp-stdio"
pinned = ["review_diff", "explain_finding", "list_rules", "health", "search"]
probe = "health"
  [behecheck.config]
  cmd = "behecheck-mcp"

[behesid]
plugin = "beheaxi-cli"
probe = "inspect"            # read-only; reads the model directory
pinned = ["validate", "inspect", "run", "series"]   # optional: bare verbs
  [behesid.config]
  cmd = "behesid"
```

- **The credential is optional, and it is still a closed set.** It is the one
  place `EnvVar(required=False)` is used: a generic plugin cannot know whether
  the backend takes one. It is named `api_key`, never `token`, because `token`
  derives `BEHEROUTER_<SURFACE>_TOKEN`, the client's gateway-bearer variable.
  `plugin-config` emits it **commented out**, so the generated block lints as
  is and nothing un-vaulted reaches the env file.
- **`mcp-stdio` takes the credential as a pair**: `api_key` in `[surface.env]`
  and `api_key_env` in `[surface.config]`. Half a pair is refused at attach,
  naming the missing half.
- **`cmd` must exist in the image** (`mcp-stdio`, `beheaxi-cli`).
  `registry-lint` warns when it is not on `PATH` where lint runs. Attach refuses
  a missing stdio command by name; a missing CLI fails attach as `Unavailable`.
- **A `beheaxi-cli` surface pins by bare verb**, and without `pinned` it uses
  the manifest's own per-verb `pinned` flags. Published names are
  `<surface>_<verb>`. The probe is required because a beheaxi manifest cannot
  declare a health verb; choose a read-only verb that touches what the CLI
  depends on.
- **Graduate to a curated plugin** once a backend's catalogue warrants versioned
  pins, search aliases and a probe that travels with the code. Generic is how a
  backend gets attached today; curated is how its knowledge stops living in one
  operator's registry file.

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

- **Per-user needs two things.** The tools' outbound HTTP client must come
  from `identity_client(...)`, an `httpx.AsyncClient` that puts the caller's
  identity headers over its own, per request. And the server must be marked
  with `mark_identity_aware(server, client)`, which **refuses** any client
  `identity_client` did not build. `openapi_server` does both for the
  `openapi` source. A surface that declares `[surface.identity]` on an
  unmarked source is **refused at attach**, never served as the deployment:
  the stdio rule, restated for `inproc`.

  ```python
  client = identity_client(base_url="https://orders.internal")
  mcp = mark_identity_aware(FastMCP("acme"), client)
  ```
- Pre-validation applies to **non-function tools only**. Function tools validate
  and coerce with pydantic, as they would over a session.

## `python-dir` — drop a file, get tools

```toml
[ops]
plugin = "python-dir"
pinned = ["restart"]
probe = "ping"
probe_args = {}
  [ops.config]
  path = "/etc/beherouter/tools/ops"
```

```python
# /etc/beherouter/tools/ops/workers.py
from fastmcp.tools import tool

@tool(name="restart")
def restart_worker(name: str) -> str:
    """Restart one queue worker by name."""
    ...
```

Every top-level `@tool` function in every `.py` file under `path` (recursive;
`__init__.py` and `_private` names skipped) becomes a tool.

- ⚠️ **This runs operator code inside the gateway process** — the same trust as
  installing a plugin package. Mount the directory read-only.
- **A surface attaches with exactly what you wrote, or not at all.** A file
  that fails to import, two functions under one tool name, or a directory with
  no tools is refused by file name. FastMCP's own directory provider skips or
  silently replaces in all three cases; `python-dir` does not use it.
- **`registry-lint` parses the files and never imports them.** It catches
  syntax errors and pins or probes that name no `@tool` function. A missing
  import is caught only at attach. If `path` is absent on the machine running
  lint, it warns instead of failing.
- **No per-user identity.** `[ops.identity]` is refused.
- Each directory's modules are private to its surface: two `python-dir`
  directories that both ship a `helpers.py` each get their own. ⚠️ Import
  siblings at module **top level**. An import inside a function body runs at
  call time, after the directory's modules have left `sys.modules`, and fails.
- No hot reload: the published tools array is frozen per process by design.
  Restart the gateway to pick up a changed file.
- On Kubernetes, mount the directory with the chart's `extraVolumes` /
  `extraVolumeMounts`; a module's third-party imports must be installed in the
  image (`plugins.install`).

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
    maturity: str = "declared"         # one of MATURITY_TIERS; see § Maturity
    evidence: tuple[str, ...] = ()     # what proves the tier, relative to the repo root
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
| `maturity` / `evidence` | The tier the plugin claims and the recorded catalogues and test ids that prove it. See § Maturity |

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

## A backend's extra text blocks: the result's `notes`

An MCP backend's reply has a structured value (`structuredContent`) and a list of content
blocks. FastMCP's first text block is just that value serialized, so the gateway forwards
the structured value as `result` and drops the duplicate. A backend may append **more**
text blocks the value does not carry — a warning to the model, such as a Plane middleware's
"NOT ASSIGNED: … do not report the assignment as done". Those travel beside the result, in
order, under `notes`:

```json
{"result": {"id": "wi-1", "assignees": ["a"]}, "notes": ["NOT ASSIGNED: b -- not a member"]}
```

The key appears only when there is a note, so every other reply is unchanged, and
`wrapped_output_schema` declares it as an optional string array (output validation passes,
a code-mode host sees it). A block is a duplicate, and not a note, when it equals the
structured value as text or as parsed JSON, or equals the `result` of FastMCP's
`{"result": v}` wrapper for a non-object return. A text-only reply has no notes: its text
is the `result`. ⚠️ **Non-text blocks (images, audio, embedded resources) are still
dropped.** Unrelated to `McpBacking.notes` below, which annotates tool *descriptions*.

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

**Every declared credential is mandatory unless it says `required=False`.** Only a generic
plugin, which cannot know whether its backend takes one, should say so. Undeclared names are
refused either way, and `plugin-config` emits an optional one commented out.

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

## Maturity

Every plugin states how much of it is **proven**, not how good it is. `beherouter plugins`
shows the tier; `registry-lint` warns on an entry whose plugin is only `declared`, naming
what would raise it. Tiers are cumulative:

| Tier | What in-tree evidence proves |
|---|---|
| `declared` | nothing: a spec exists. Every generic plugin, and anything `catalog-import` produced |
| `probed` | the default `probe` is in a cited recorded catalogue, and its `probe_args` (or `{}`) validate against that tool's `inputSchema` |
| `catalogued` | every pin and every `search_aliases` key is in that catalogue |
| `verified` | an `evidence` id resolves to an e2e check: `path::test_fn`, or `tests/e2e/e2e.py::<check name>` for the driver script |
| `per-user` | the plugin declares `IdentitySupport` **and** cites an e2e check asserting `matches_caller` |

The in-tree tiers today: `plane-http` per-user; `plane` and `plane-http-apikey` verified;
`office-mcp`, `sonarqube`, `gcal` and `m365` catalogued; the generic plugins declared.

Declare it on the spec and prove it in your test suite:

```python
SPEC = PluginSpec(..., maturity="catalogued",
                  evidence=("tests/catalogues/acme-2.1.0.json",))

from beherouter.testing import plugin_conformance

def test_my_plugin_meets_its_tier():
    report = plugin_conformance(SPEC)          # root defaults to the cwd
    assert report.ok, report.problems
```

`tests/test_maturity.py` runs exactly this over every in-tree plugin, so a tier cannot
outlive its evidence. Record a catalogue with `scripts/record_catalogue.py`.

- ⚠️ **Evidence must resolve, even when the tier is otherwise met.** A cited file or test
  id that has gone away fails conformance: stale evidence is how a tier silently stops
  being true.
- ⚠️ **Conformance cannot check that a cited e2e check exercises *this* plugin.** The id
  must exist, and for `per-user` must assert `matches_caller`; that it is the right check is
  a code-review question, which is why the id is spelled out on the spec.
- ⚠️ **An out-of-tree plugin is shown as `probed` at most** unless every evidence path it
  cites ships in its own distribution. The gateway runs nobody's test suite, so a claim it
  cannot see the evidence for is displayed as the claim it can be checked down to, with a
  note saying why.

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

`attach <surface> <plugin> --config k=v,k=v` writes the same block into `registry.toml`
**in place**, keeping the file's comments and layout (`detach` likewise). Values are
coerced: integers, floats and `true`/`false` become TOML types; repeat a key for a list.

## Importing from the MCP Registry

The MCP Registry's `server.json` says how to **reach or launch** a server and which secrets
it needs. It carries no tool list, no pins and no probe, so it is an import *source* for the
generic plugins, never a plugin of its own:

```bash
beherouter catalog-import ./server.json acme               # a file
beherouter catalog-import https://…/server.json acme        # a URL
beherouter catalog-import io.github.acme/acme-mcp@1.4.0 acme   # a registry name
beherouter catalog-import io.github.acme/acme-mcp acme --registry https://registry.internal
```

The verbs are flat and the surface is positional (beheaxi renders a required parameter as
an argument, as `plugin-config <surface> <plugin>` does). A `remotes[]` entry becomes an
`mcp-http` block, a `packages[]` entry an `mcp-stdio` one; like `plugin-config`, the output
is the registry, proxy and env fragments, with a placeholder and never a credential.

- ⚠️ **`pinned` and `probe` come out as TODOs that fail `registry-lint`**, so an imported
  entry cannot be committed until someone has chosen them — the `gitea-home` rule surviving
  the import path. A server that publishes an `io.beherouter/plugin` block under `_meta`
  (`io.modelcontextprotocol.registry/publisher-provided`) gets them filled in instead. The
  block is **advisory**: it becomes entry overrides an operator reviews, never trusted spec
  data, and its identity modes count only where the target plugin already declares them.
- ⚠️ **A package is pinned to its exact version**, never `latest`: an unpinned `npx` is a
  different server on every restart, behind a probe chosen for the old one.
- ⚠️ **The published image has no Node and no `uvx` alias**, so an imported `npm` or `pypi`
  package needs a derived image (or a `cmd` override); the import says so.
- ⚠️ **A generic plugin carries one credential (`api_key`).** A server declaring more is
  imported with the first required one and a warning naming every other — a missing
  second secret attaches green and fails on the first call that needs it.

The reverse, for a private subregistry: `beherouter catalog-export <plugin>` emits a curated
plugin as a `server.json` with its pins, probe and aliases in that `_meta` block
(`--name`, `--server-version`, `--url`, `--package` fill what the spec cannot know).
The plugin's credentials are **not** described — how the plugin sends one lives in its
`build()` — so export warns, naming them, and you add `remotes[].headers` or
`packages[].environmentVariables` yourself. What is imported is `declared`
(§ Maturity) until a curated plugin proves more.

## Four warnings

⚠️ **A surface that fails to attach is isolated, not fatal.** A `build()` that raises, or
an attach that takes longer than `BEHEROUTER_ATTACH_TIMEOUT_S` (default 30 s), leaves that
one surface answering RFC 9457 `503` while the gateway retries it in the background (5 s,
doubling to 300 s) and swaps it in on the first success. `/healthz` stays HTTP 200 but
reports `"status": "degraded"` and lists the surface under `failed`; the error itself is
only in the container logs. A **configuration** fault found at attach (a `UsageError`, such
as a bad `[surface.identity]`) is not retried — it will not fix itself — so the surface also
appears under `needs_config_change` and its `503` says it will not be retried. What
`registry-lint` can see — an unknown plugin, a bad config value, an unset `${VAR}` —
**still refuses boot**, so run it before a deploy.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty `${VAR}` is
a `UsageError` raised during `build_surfaces` — i.e. at *startup* — so it does not yield a
broken surface, it yields a **dead gateway**, `/healthz` included. Entry and secret ship
together, or not at all.

⚠️ **Probe before pinning, and re-probe the whole list after an upgrade or an edition
change.** A backend's catalogue advertises the **commercial** surface, so "the tool exists"
says nothing about whether *this* deployment serves it. Since 2026-09-10 `health --deep`
fails a surface whose pin list names a tool the backend no longer serves (`catalogue:
"pinned_missing"`), and `/healthz` lists such pins under `pinned_missing` from attach on
(a WARNING, not a status change), so the *existence* half is mechanical — proving a served tool still
**works** is what `probe` is for.

⚠️ **One backend credential means one identity — unless the surface declares
otherwise.** Every write through a surface with no `[surface.identity]` table is
attributed to the single account whose token or consent it carries, and making that
surface available to every user did not make it per-user.

A plugin can opt into per-user calls by declaring `IdentitySupport` on its spec:
which of the five modes it can carry, which slot they land in, and which target
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

- ⚠️ **An entry point that fails to import is logged, recorded and skipped**, not
  fatal. One broken third-party dependency must not cost the gateway every other
  plugin. It is not silent either: `beherouter plugins` lists it under `failed`,
  `registry-lint` warns about it, and a registry naming it fails with
  `unknown plugin` — an error that names the entry points that failed to load,
  since that is the likeliest reason.
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
  More indexes (`plugins.indexes[]`) and wheels from a ConfigMap
  (`plugins.local`) go through the same installer; see `docs/DEPLOYMENT.md`
  § Private CA, out-of-tree plugins.

### The plugin API and its version

Import **only** from `beherouter.plugin_api`. It is the one import path that
carries a compatibility promise; `beherouter.plugins`, `beherouter.backends.*`
and the rest stay importable and promise nothing. It exports exactly:

`API_VERSION`, `AuthError`, `Backend`, `CliBacking`, `ConfigField`, `EnvVar`,
`IdentitySupport`, `McpBacking`, `PluginContext`, `PluginSpec`,
`ToolDescriptor`, `Unavailable`, `UsageError`, `identity_client`,
`load_cli_backend`, `load_inproc_backend`, `load_mcp_backend`,
`mark_identity_aware`, `register`.

`PluginSpec.api` defaults to the current `API_VERSION` (**1**), so a plugin
states nothing to be current. The number moves only on a change an existing
plugin cannot survive; additive fields keep it. `register()` refuses a plugin
written for a version this gateway does not serve, naming both versions. An
entry-point plugin refused that way is logged and skipped like any other that
fails to import, so a registry naming it then fails `registry-lint` as an
`unknown plugin`.

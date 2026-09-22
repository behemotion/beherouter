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

## The four backings

| Backing | What it is | Choose it when |
|---|---|---|
| `http` | An MCP server reached over HTTP | The backend already runs as a service |
| `stdio` | An MCP server run as a subprocess | You would otherwise add a container for it — a subprocess costs a process instead. Also sidesteps remote transports that 401 an un-credentialed probe, which some clients misread as "this server wants OAuth" |
| `cli` | A beheaxi CLI, described and invoked as tools | The backend is a command-line tool following the harness CLI contract |
| `native` | In-process Python | No sidecar, no extra runtime, no writable state — the calendar plugins are these |

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
```

| Field | Meaning |
|---|---|
| `name` | The name used in `registry.toml` as `plugin = "<name>"` |
| `summary` | One line, shown by `beherouter plugins` |
| `backing` | `"http"`, `"stdio"`, `"cli"` or `"native"` |
| `pinned` | The tools published in the surface's `tools` array |
| `probe` / `probe_args` | The tool `health --deep` calls to prove the backend really works |
| `config` | The keys allowed in `[surface.config]` |
| `env` | Credentials, by **logical** name |
| `catalogue_ttl_ms` | How long the searchable catalogue stays fresh |

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

Pin resolution has exactly **one** implementation, `resolve_pinned(entry, plugin)`, for the
same reason: two call sites deciding the same thing separately is how one gets enriched and
they diverge.

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

⚠️ **An attach failure crash-loops the whole gateway**, taking every other surface and
`/healthz` with it. `registry-lint` is the pre-deploy guard; the container logs are how you
find out which backend did it.

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

Not supported yet. In-tree plugins import themselves at the bottom of
`plugins/__init__.py`. The protocol is designed so that out-of-tree discovery is a change of
**lookup only** —

```python
for ep in entry_points(group="beherouter.plugins"):
    ep.load()
```

— with no change to `PluginSpec` or `build()`.

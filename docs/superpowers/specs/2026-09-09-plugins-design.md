# Plugins — pre-configured, pluggable MCP backends — design

**Date:** 2026-09-09
**Status:** design approved in chat; **nothing implemented.**
**Supersedes (architecture only):** `2026-09-09-calendar-plugin-design.md` and its
implementation plan `../plans/2026-09-09-calendar-plugin.md`. That design's
*domain* content — the six calendar tools, the time rule, the OAuth traps, the
out-of-scope list — is carried forward here verbatim in intent and is still
authoritative on calendars. What it got superseded on is the **seam**:
`kind = "native"` becomes one *backing* of a general plugin, not a third backend kind.

## The motto

beherouter has two faces, and every design decision serves one of them:

> **To agents:** a thin, curated MCP surface — a short pinned tool list plus fuzzy
> search (`search_tools` / `describe_tool` / `run_tool`) that locates the long tail
> on demand, so a client pays a handful of tool definitions instead of hundreds.
>
> **To backends:** pluggable, pre-configured MCP servers — every backend is a
> versioned, tested plugin carrying its own pins, probe, config schema, credential
> names and quirk workarounds, so attaching one is a name in `registry.toml`, not
> an act of archaeology.

The first face has existed since 2026-07-28 and works. This design builds the second.

## Problem

Attaching a backend today is archaeology, and the evidence is measurable:

- **Three edits across two repos**, in a fixed order, none of them checked:
  a `registry.toml` block here, a `not` clause in the homelab `Caddyfile`, and a
  token line in `beherouter-env.j2`. The documented failure modes are asymmetric and
  bad: registry-only → the surface is unreachable (default-deny, safe); registry +
  env without the Caddy clause → **it answers to another client's token** (not safe).
- **The curation knowledge is untested prose in another repo.**
  `$HOMELAB_REPO/ur/service/beherouter/registry.toml` is ~16 KB, overwhelmingly
  comments: which 11 of Plane's 30 tools are worth pinning, that `plane_api_1` must
  be addressed as `plane-api`, that five catalogue tools 404 on Community Edition,
  that `get_pql_reference` is uncallable upstream. Every one of those is a
  behavioural claim that a test could enforce and a comment cannot.
- **A bad registry is discovered in production.** An attach failure crash-loops the
  gateway, taking every other surface and `/healthz` with it (observed 2026-08-04).
  There is currently no way to find out whether a registry edit is valid except to
  deploy it.
- **There is no way to expose a capability that is simply Python.** Both existing
  kinds (`cli`, `mcp`) require an external process. Inherited verbatim from the
  calendar design, where it is the reason calendars cannot be built at all today.

## Decisions settled 2026-09-09

| # | Decision | Consequence |
|---|---|---|
| 1 | A plugin is a **kind-agnostic pre-configured recipe** — it declares pins, probe, config schema, credentials **and its backing** | `kind` disappears from the operator's vocabulary |
| 2 | The plugin **declares its plumbing**; a generator emits the Caddy clause and env line | Mirrors `client-config`; the three fragments cannot disagree |
| 3 | **Plugins are the only way.** `office`, `plane`, `gcal`, `m365` all migrate now | `kind`/`url`/`cmd`/`transport` are removed from `registry.toml` |
| 4 | **In-tree now**, entry points as a declared extension on the same protocol | `PLUGINS` is a dict today; a loop over `entry_points()` later changes only the lookup |

## Architecture

### The plugin contract — inert data plus one build function

A plugin registers two things. The first is frozen data that cannot open a socket
or import a provider SDK:

```python
# src/beherouter/plugins/spec.py
@dataclass(frozen=True)
class ConfigField:
    name: str
    type: type                      # str | int | bool | float
    required: bool = False
    default: object = None
    doc: str = ""                   # rendered by `plugin describe`

@dataclass(frozen=True)
class EnvVar:
    name: str                       # LOGICAL name, e.g. "api_key"
    doc: str = ""

@dataclass(frozen=True)
class PluginSpec:
    name: str                       # "plane", "gcal"
    summary: str                    # one line, for `plugin list`
    backing: str                    # native | http | stdio | cli
    pinned: tuple[str, ...] = ()
    probe: str | None = None
    probe_args: dict | None = None
    config: tuple[ConfigField, ...] = ()
    env: tuple[EnvVar, ...] = ()
```

The second is a build function, `async (PluginContext) -> Backend`, where
`PluginContext` carries the surface name, the validated config, the resolved
credentials, and the effective pin list.

```python
register(spec, build)
PLUGINS: dict[str, Plugin]          # Plugin = (spec, build)
```

**Why the split is load-bearing.** Three consumers must read a plugin's declaration
without attaching anything: the plumbing generator, `registry-lint`, and
`plugin list` / `plugin describe`. Keeping the declaration inert is also what makes
the calendar design's rule — *attach must perform no network I/O* — mechanically
enforceable rather than a convention: a plugin **cannot** phone home from its spec,
only from `build`.

### Backings

| Backing | Meaning | Built by |
|---|---|---|
| `http` | an MCP server reachable over HTTP | existing `load_mcp_backend` |
| `stdio` | an MCP server run as a subprocess | existing `load_mcp_backend` |
| `cli` | a beheaxi CLI, verbs executed as subprocesses | existing `load_cli_backend` |
| `native` | in-process Python; the plugin *is* the implementation | the plugin's own `build` |

Backing-specific values (a URL, a command line) are **`ConfigField`s with
plugin-supplied defaults**, not fields on `PluginSpec`. That is what keeps a plugin
pre-configured while remaining deployable somewhere else, and it keeps `PluginSpec`
free of per-backing keys.

### One refactor: separate the public schema from the loader input

`load_mcp_backend` and `load_cli_backend` currently take a `RegistryEntry` — the
same type as the public TOML schema. Since that schema is losing `kind`/`url`/`cmd`,
the loaders start taking internal `McpBacking` / `CliBacking` dataclasses that a
plugin's `build` constructs. Public config and internal loader input stop being one
object, so neither constrains the other.

### The load path

```
registry entry ──► PLUGINS[entry.plugin]         unknown plugin        -> UsageError
               ──► validate entry.config          unknown/missing/type -> UsageError
               ──► envexpand(entry.env)           unset or empty ${VAR}-> UsageError
               ──► pins   = entry.pinned or spec.pinned
                   probe  = entry.probe  or spec.probe
               ──► await build(ctx) -> Backend    (the only step that may do I/O)
```

`${VAR}` expansion is promoted out of `backends/mcp.py` (where it is a private
`_expand`) into a shared `envexpand` module, so every backing gets identical
semantics: whole-value placeholders only, unset-or-empty is a `UsageError`, never
an empty credential. This is carried over from the calendar design and is the rule
that prevents the `gitea-home` failure shape (a credential that attaches fine and
then fails every call).

### `registry.toml` after

```python
@dataclass
class RegistryEntry:
    name: str
    plugin: str                        # the ONLY required key
    config: dict | None = None
    env: dict[str, str] | None = None  # logical credential -> ${VAR}
    pinned: list[str] | None = None    # override a tested default
    probe: str | None = None
    probe_args: dict | None = None
# removed: kind, cmd, transport, url
```

The live 16 KB file becomes, at cutover, these nine lines:

```toml
[office]
plugin = "office-mcp"

[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "homelab"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_TOKEN}"
```

⚠️ **A registry entry may not be added before its secret exists.** An unset or empty
`${VAR}` is a `UsageError` by design — but that error is raised during
`build_surfaces`, i.e. at startup, so it **crash-loops the whole gateway and takes
`/healthz` with it**. A `[gcal]` block committed before
`BEHEROUTER_GCAL_REFRESH_TOKEN` is in the vault does not yield a broken calendar
surface; it yields a dead gateway. Entry and secret ship in the same playbook run,
or not at all. `registry-lint` checks this against the *local* environment, so it
catches the ordering mistake only when run where the variables exist — the ordering
rule is the real guard, and lint is the backstop.

Nothing is deleted. The knowledge changes **form**: from prose an operator must read
and obey, into code the test suite enforces.

### Credentials become logical

`env` keys are names the *plugin* declares (`api_key`), not the literal variable the
backend happens to read. The plugin maps a logical name to wherever it actually goes
— `PLANE_API_KEY` in a subprocess environment, an `Authorization` header, an OAuth
token exchange. The operator never needs to know that Plane's SDK reads
`PLANE_API_KEY`, which is exactly the class of knowledge that today exists only in a
comment.

⚠️ **`pinned` and `probe` in a registry entry are OVERRIDES of a tested default, not
required knowledge.** Forgetting them now yields the plugin's verified behaviour
rather than an unprobed surface — the `gitea-home` mistake becomes unmakeable by
default.

## The four plugins

### `office-mcp` — backing `http`

Config: `base_url`, default `http://office-mcp:8100/mcp/`. Pins all four tools
(`discover`, `invoke`, `file_from_url`, `job_status`); probe `discover` with
`{query: "pdf"}` — none of office-mcp's tools take zero arguments, which is why
`probe_args` exists. No credentials: office-mcp has no app-level auth and is reached
over the shared `behe-gateway` network rather than through Caddy.

Its docstring records the honest fact that this surface is a **pass-through** — it
saves no context, because office-mcp already does its own pinned-few + search split
internally. The benefit is credential centralisation and one uniform client surface.

### `plane` — backing `stdio`

Config: `base_url` (default `http://plane-api:8000`), `workspace_slug` (required),
`cmd` (default `/opt/plane-mcp/bin/plane-mcp-server stdio`). One logical credential,
`api_key` → `PLANE_API_KEY`. The 11 pins and `probe = member` / `{action: "me"}`
become spec defaults.

Three comments become enforced invariants:

| Today (prose, in another repo) | Becomes |
|---|---|
| "Django 400s a `Host` containing an underscore, before `ALLOWED_HOSTS` is consulted" | `base_url` validation rejects an underscore in the host — offline, at lint time |
| "`page`, `work_log`, `milestone`, `workitem_type`, `initiative` 404 on Community Edition" | `test_plane_pins_exclude_community_edition_gaps` |
| "`get_pql_reference` is uncallable in 0.3.2 (schema declares `detail`, dispatcher demands `action`)" | `test_plane_does_not_pin_get_pql_reference` |

The remaining prose — why stdio rather than http on a host with ~460 MB free, why
not the web port (a Next.js catch-all answers an un-credentialed API path with 200
and HTML), why not the public vhost — moves into the plugin module's docstring,
where it is versioned with the code it explains.

⚠️ **Every write through this surface is attributed to one Plane identity.** Carried
forward unchanged; the plugin does not alter it.

### `gcal` and `m365` — backing `native`, two plugins over one core

Two plugins, two independently-attachable surfaces, two Caddy tokens, two
credentials — over a **single shared core** at `plugins/calendar/` that owns the six
tool descriptors, the time rule, the OAuth refresh and response normalisation. Each
plugin is a provider adapter plus a spec.

This resolves the superseded design's "one plugin or two" question rather than
picking a side. The thing that must not fork is the **agent-facing vocabulary**; the
things that benefit from forking are the credential, the surface and the token. A
test asserts both plugins expose **byte-identical tool schemas**, so drift is
impossible rather than merely discouraged.

Carried forward from the calendar design, unchanged and still authoritative:

- **Six tools:** `list_calendars`, `list_events`, `get_freebusy`, `create_event`,
  `update_event`, `delete_event`. The complete book-and-adjust loop and no more.
- **No `get_current_time`** — the surface's instructions carry the clock; a tool
  round-trip to read one is waste.
- **Every timestamp is RFC 3339 with an explicit offset.** Naive input is rejected
  with a `UsageError` naming the rule, converting a silent wrong-hour booking into a
  loud error the agent can retry.
- **Attach performs no network I/O.** The first token fetch happens on the first
  `run()`, so a dead refresh token fails a *probe* instead of crash-looping the
  gateway.
- **Access tokens are never persisted** — in memory only, so the container keeps its
  no-writable-state property.
- ⚠️ **Google:** the OAuth consent screen must be published to **Production**. Left
  in Testing, refresh tokens expire after **7 days**.
- ⚠️ **Microsoft:** the token endpoint must be the **`consumers`** authority. The
  default `common` authority issues refresh tokens rejected at first refresh — it
  works for about an hour and then dies.
- **Microsoft free/busy derives from `calendarView`, not `getSchedule`**, which is
  documented for work/school accounts and is not dependable on a personal one.
- ⚠️ **Bootstrap is out of band.** The first refresh token comes from an interactive
  consent flow with a browser, which the gateway will not host. A gateway that can
  perform OAuth consent is a gateway with a login UI, session state and redirect
  URIs, and none of that belongs in a backend router.

### The `cli` backing ships with no plugin

Nothing live uses it, but `CLIExecutor` is written and tested. It remains an
available backing, exercised by a test-only plugin so the code does not rot.

## Plumbing generation

`beherouter plugin-config <surface> --plugin <name>` emits all three fragments,
derived from the inert spec so they cannot disagree:

```
# --- registry.toml ---
[gcal]
plugin = "gcal"
  [gcal.env]
  refresh_token = "${BEHEROUTER_GCAL_REFRESH_TOKEN}"

# --- ur/service/Caddyfile (inside the beherouter vhost matcher) ---
  not path_regexp gcal ^/gcal/mcp/?$

# --- ansible/playbooks/templates/beherouter-env.j2 ---
BEHEROUTER_GCAL_REFRESH_TOKEN={{ vault_beherouter_gcal_refresh_token }}
```

It follows `client-config`'s rules exactly: **never emit a credential, only a
placeholder**, and emit the vault variable *name* rather than any value. Env lines
are generated only for plugins that declare `EnvVar`s — `office-mcp` declares none
and correctly produces a two-fragment output.

`beherouter registry-lint <path>` validates a registry file against the installed
plugins with **no network and no attach**: unknown plugin, unknown or missing config
key, wrong type, unset `${VAR}`, and the plugin's own config validators (such as
plane's underscore rule). This turns a production-outage-shaped feedback loop into a
local one, and is only possible because `PluginSpec` is inert.

## Cutover and rollback

The strict unknown-key check in `load_registry` means the old registry is invalid
under new code and the new registry is invalid under old code. There is therefore
**no half-state to get stuck in**, provided both ship together — and they do:
`ansible-playbook playbooks/service.yml --tags beherouter` ships image, source and
`registry.toml` in one run.

1. `registry-lint` the new registry locally; full test suite green.
2. Re-vendor `src/` into `$HOMELAB_REPO/ur/service/beherouter/`, write the new
   `registry.toml`, commit both in the homelab repo.
3. One `--tags beherouter` run. **The Caddyfile playbook is only needed if a surface
   path is added or removed** — the office/plane migration changes neither.
4. Verify: `/healthz` lists the expected surfaces, and
   `podman exec beherouter beherouter health --deep --json` reports `attach: ok` +
   `probe: ok` for `office` and `plane`.
5. Rollback is reverting both homelab commits and re-running the same playbook.

`gcal` and `m365` are **not** part of this cutover. They attach only once their
vault secrets exist, so the office+plane migration ships and is verified before any
OAuth bootstrap is attempted.

## Delivery phases

The spec is one design but two shippable units, and they must not be merged into one
deployment:

| Phase | Contents | Ships when |
|---|---|---|
| **1 — the seam** | `PluginSpec`/`register`, the backing split, `envexpand`, `registry-lint`, `plugin-config`, the `office-mcp` and `plane` plugins, the registry migration | Immediately. Adds **no new capability** — it must reproduce exactly the two surfaces already live, which is what makes it safely verifiable |
| **2 — calendars** | `plugins/calendar/` core, `gcal`, `m365`, the out-of-band OAuth bootstrap procedure | After Phase 1 is verified live, and only once each surface's secrets are in the vault |

Phase 1 is a refactor with a green/green test: the same two surfaces, the same
probes, the same `/healthz` output, from a registry that is 16 KB smaller. Phase 2 is
the first genuinely new capability, and it cannot destabilise Phase 1 because a
calendar entry is simply absent from the registry until its credentials exist.

## Testing

- The existing 176 tests stay green; `beheaxi conformance "beherouter"` stays 6/6.
- Per-plugin spec tests: defaults, required config, type rejection, env mapping.
- The three `plane` invariants in the table above.
- `gcal` and `m365` expose byte-identical tool schemas.
- `registry-lint` cases, one per rejection reason.
- **No `PluginSpec` import opens a socket** — the enforcement of no-I/O-at-attach.
- `office` and `plane` plugin builds tested against recorded catalogues, so the
  migration is proven to produce the same surfaces it replaces.

## Deliberately out of scope

- **Per-user OAuth and multi-account surfaces.** One credential per registry entry,
  exactly like the Plane PAT. Two accounts = two entries.
- **Recurring-event creation, attachments, reminders, colours, ACLs, calendar
  creation, mail/files/contacts/tasks, room booking.** Carried over verbatim.
- **Out-of-tree plugin discovery.** The protocol is designed for it; the loader is
  not built yet (decision 4).
- **Moving the per-surface token boundary out of Caddy.** Considered and rejected:
  it would make the gateway hold every client token and turn an `auth.py` bug into a
  cross-tenant leak rather than a 401.
- **Replacing the gateway's web stack.** beherouter runs on **Starlette + uvicorn**
  with no ORM and no database. FastAPI is Starlette plus a pydantic layer, so
  adopting it would add weight, not remove it. ⚠️ The Django named in this document
  is **Plane's own backend**, a third-party container we front — not code in this
  repo. Replacing *Plane* with something lighter (behetask is the obvious candidate)
  is a homelab decision, and this design is what would make that swap cheap: the
  surface becomes `plugin = "behetask"` and every consumer keeps its URL and token.

## Open questions the implementer must settle by measurement

1. Does `get_freebusy` need the broad `calendar` scope, or does `calendar.events`
   suffice? Upstream documents neither.
2. Exact `list_events` page sizes either provider enforces before paginating.
3. ~~Whether `save_registry` should round-trip a `config` table containing nested
   tables, or reject them.~~ **Settled 2026-09-25: both.** Nested tables of
   any depth round-trip (`[x.identity.map]` was being written as a repr string
   by `attach`/`detach`); a value TOML-by-hand cannot hold (an array of tables)
   raises `UsageError` before the file is touched. Comments are still not
   preserved. Flat typed scalars are required either way — `_toml_str`
   currently serializes strings only, which would silently write `50` as `"50"`.

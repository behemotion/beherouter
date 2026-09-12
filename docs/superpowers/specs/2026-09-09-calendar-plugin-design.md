# Native plugin backends, and a calendar plugin — design

> ⚠️ **SUPERSEDED ON ARCHITECTURE (2026-09-09) by
> [`2026-09-09-plugins-design.md`](2026-09-09-plugins-design.md).** `kind = "native"`
> is not being built as a third backend kind; it became one *backing* of a general
> plugin seam, and `gcal`/`m365` are two plugins over a shared calendar core rather
> than one `calendar` plugin with two providers.
>
> **This document is still authoritative on calendars themselves** — the six tools,
> the RFC 3339 time rule, the OAuth traps, and the out-of-scope list are carried
> forward unchanged. Read it for the domain; read the plugins design for the seam.

**Date:** 2026-09-09
**Origin:** homelab repo. Research that produced this:
`$HOMELAB_REPO/docs/librechat-calendar-integration.md`.
**Status:** design approved in the homelab session; **nothing implemented**.

## Problem

LibreChat agents can produce meeting artefacts (protocols, task lists — see
`babylon/web/demo-unibank/`) but cannot **book or adjust a real meeting**. The
consumer wants Google Calendar and Office 365 (a **personal outlook.com**
account, decided 2026-09-09 — not a corporate tenant, so there is no Azure
admin-consent problem).

The obvious route is two off-the-shelf Node MCP servers (`nspady/google-calendar-mcp`,
`softeria/ms-365-mcp-server`) as sidecar containers on `behe-gateway`, attached
as ordinary `kind = "mcp" / transport = "http"` surfaces. That was researched in
full and works. **It was rejected by the user in favour of native plugins.**

## Why native, and the cost of it

Rejecting the sidecars buys:

- **No Node runtime.** `ur/service/beherouter/Containerfile` is
  `FROM python:3.12-slim`. Sidecars keep it that way but add two containers;
  stdio subprocesses avoid the containers but put Node *in* the image.
- **No new tenants.** Two compose projects, two named volumes, two ansible
  tags, two sets of resource limits — none of it needed.
- **One token store, one deploy, one image.**
- **A third backend kind the gateway is missing.** Today `KINDS = ("cli", "mcp")`.
  Both require an *external* process. There is no way to expose a capability
  that is simply Python — which is a gap independent of calendars.

It costs: OAuth refresh, pagination, recurrence expansion and timezone handling
become **ours**. Those are the genuinely hard parts of the upstream servers, and
this design does not pretend otherwise — §"Deliberately out of scope" names what
is not being reimplemented.

⚠️ This is in tension with AGENTS.md's *"adding a service = a config entry +
optional per-service middleware, **not** a new bespoke wrapper file."* The
tension is resolved by making the **seam** generic (`kind = "native"`, any
plugin) and the calendar plugin its first tenant, rather than special-casing
calendars in the gateway. A second native plugin must need zero gateway changes.

## Architecture

### The seam: `kind = "native"`

`models.Backend` is already the whole contract:

```python
@dataclass
class Backend:
    name: str
    kind: str
    descriptors: list[ToolDescriptor]
    executor: "Executor"          # async run(verb, args) -> dict
```

`surface.py` builds an MCP surface from that and does not care where the tools
came from. So a native plugin is just **an object that produces descriptors and
answers `run()`**. No new concepts.

```
registry.toml                       gateway.load_backend()
[gcal]                       ->     kind == "native"
kind    = "native"                    -> backends/native.py
plugin  = "calendar"                     -> PLUGINS["calendar"](entry, config)
                                            -> Backend(descriptors, executor)
```

**Three registry additions**, all optional and ignored by other kinds:

| Key | Meaning |
|---|---|
| `plugin` | which native plugin to instantiate (required when `kind = "native"`) |
| `config` | a plugin-specific table; `${VAR}` values expand like `env` does |

`env` is reused as-is for credentials. `${VAR}` expansion already exists but is
**private to `backends/mcp.py`** (`_expand`); it gets promoted to a shared module
so the native loader shares the exact same semantics — whole-value placeholders
only, unset-or-empty is a `UsageError`, never an empty credential.

### The plugin: one `calendar`, two providers

**Not** two plugins. One `calendar` plugin with two provider adapters, selected
per registry entry:

```toml
[gcal]
kind = "native"
plugin = "calendar"
probe = "list_calendars"
  [gcal.config]
  provider = "google"
```

Rationale:

- **Agents get one vocabulary.** `create_event` behaves identically whichever
  provider backs it. Two independent plugins would drift.
- **The booking logic is shared and is the valuable part** — normalising times,
  turning free/busy windows into candidate slots, validating that end > start.
  Only the HTTP calls differ.
- **A third provider (CalDAV, Fastmail) is then an adapter, not a plugin.**

Two registry entries attach it twice, as two surfaces (`/gcal/mcp`, `/m365/mcp`),
each with its own Caddy token. That falls out of the existing model for free.

### Tool surface — six tools, deliberately

| Tool | Purpose |
|---|---|
| `list_calendars` | discover calendar ids; also the credential probe |
| `list_events` | read a window |
| `get_freebusy` | **the tool that makes booking real** rather than guesswork |
| `create_event` | book |
| `update_event` | adjust — move, rename, re-invite |
| `delete_event` | cancel |

Six is the complete book-and-adjust loop and no more. The upstream servers
expose 12 and 300+ respectively; most of that is mail, files, contacts and
tasks, none of which was asked for. **YAGNI is doing real work here** — every
extra tool is context every agent pays for on every call.

No `get_current_time` tool: the gateway can put the current time and timezone
into the surface's instructions, and a tool round-trip to read a clock is waste.

### Time handling — one rule, enforced at the boundary

Every timestamp crossing the tool boundary is **RFC 3339 with an explicit
offset** (`2026-09-15T14:00:00+06:00`). Naive local times are rejected with a
`UsageError` naming the rule.

This is not fussiness. Both provider APIs accept a naive `dateTime` plus a
separate `timeZone` field, and a model that omits the timezone silently books in
whatever the provider defaults to — the failure is a meeting at the wrong hour,
discovered by a human, days later. Rejecting naive input converts a silent
wrong-answer into a loud error the agent can retry.

### Credentials

Both providers use **refresh-token OAuth**, not a static PAT. Refresh tokens live
in `env` as `${VAR}` placeholders, resolved from the gateway's environment (i.e.
`~/beherouter/.env` ← ansible vault) exactly like the Plane PAT.

**Access tokens are never persisted.** They are fetched from the refresh token at
attach time and re-fetched on expiry, in memory. This is the design's single
biggest simplification versus the sidecars: **no token-cache volume, so the
gateway keeps its "no writable state" property**, which `podman-compose.yml`
currently guarantees with a single read-only mount.

The cost is that a refresh token revoked upstream breaks the surface with no
local recovery — which is correct, and is exactly what `probe` exists to surface.

**⚠️ Bootstrap is out of band.** Obtaining the first refresh token needs an
interactive consent flow with a browser, which the gateway will not host. It is a
documented one-time human procedure (see the plan's Task 11), producing a string
that goes into the vault. This is deliberate: a gateway that can perform OAuth
consent is a gateway with a login UI, session state and redirect URIs, and none
of that belongs in a backend router.

Two traps carried forward from the homelab research, both upstream-documented:

- **Google:** the OAuth consent screen must be published to **Production**. Left
  in Testing, refresh tokens expire after **7 days**.
- **Microsoft:** the token endpoint must be the **`consumers`** authority. The
  default `common` authority issues refresh tokens that are *rejected at the
  first refresh* — it works for about an hour and then dies.

### Failure behaviour

⚠️ **An attach failure crash-loops the whole gateway**, taking every other
surface and `/healthz` with it (AGENTS.md; observed 2026-08-04).

So: **attach must not perform network I/O.** A plugin builds its descriptors from
static declarations and returns; the first token fetch happens on the first
`run()`. A dead refresh token therefore produces a failing *probe*, never a
crash-looping gateway.

This is a deliberate inversion of the `mcp` backend, which must talk to its
backend at attach time to learn the catalogue. A native plugin knows its own
catalogue, so it has no excuse to.

## Deliberately out of scope

Named so nobody implements them by accident, and so the boundary against the
upstream servers is honest:

- **Recurring-event *creation*.** Reading an expanded series is in; authoring an
  RRULE is not. Ask the user to create the series by hand.
- **Attachments, reminders/overrides, colours, ACLs, calendar creation.**
- **Mail, files, contacts, tasks, Teams, SharePoint.** Not calendars.
- **Room/resource booking.** Personal accounts do not have rooms.
- **Multi-account per surface.** One credential per registry entry. Two accounts
  = two entries, which the surface model already supports.
- **Per-user OAuth.** One shared identity per surface, exactly like the Plane
  PAT. Noted in the homelab research as the wrong shape for a public demo; if
  per-user calendars are ever required that is a different design.

## Open questions the implementer must settle by measurement

1. ~~**Does Graph `getSchedule` work on a personal outlook.com account?**~~
   **CLOSED by the plan (Task 9): it is not used.** `getSchedule` is documented
   for work/school accounts and is not dependable on a personal one, so
   Microsoft free/busy is derived from `calendarView` instead — one call, always
   available, identical normalised output. Revisit only if a work/school account
   is ever added, where `getSchedule` would also cover other people's calendars.
2. **Does `get-freebusy` need the broad `calendar` scope**, or does
   `calendar.events` suffice? Upstream does not say.
3. **Exact `list_events` page sizes** either provider enforces before paginating.

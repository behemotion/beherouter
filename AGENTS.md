# beherouter — multi-service MCP gateway

> **Status: LIVE, fronting `office-mcp` + Plane. Renamed behemcp → beherouter 2026-08-02.**
> Deployed at **`https://beherouter.example.com`** (the old `behemcp.example.com` was
> cut over with no redirect — it no longer resolves) — on the **service** VM (198.51.100.114), *not*
> dev-4, and deployed from the **homelab** repo
> (`$HOMELAB_REPO/ur/service/beherouter/`), not by this repo's `docs/DEPLOYMENT.md`.
> `$DEV4_SSH_PASS` was never needed and the beheaxi-v0.1.0 blocker is closed
> (released as **v0.1.1**, pinned here).
>
> **Two surfaces are attached: `office` (2026-08-04) and `plane` (2026-09-08).**
> `/healthz` → `{"status":"ok","surfaces":["office","plane"]}`, and
> `beherouter health --deep --json` reports `attach: ok` + `probe: ok` for both.
> Each probe is a real credentialed `tools/call`, not a catalogue listing —
> `office` was additionally verified by an agent calling `discover` end-to-end
> through `https://beherouter.example.com/office/mcp`. What each surface is, and the
> traps each carries, is **§ Plugins** below.
>
> **Both surfaces are attached through plugins since 2026-09-09** (Phase 1).
> `kind`/`url`/`cmd`/`transport` are gone from `registry.toml`; an entry is a
> plugin name plus overrides, and the live file is nine lines instead of 16 KB.
> `gitea-home` was removed on 2026-07-30 (revoked PAT: it listed and searched
> perfectly while every tool call failed) and has **not** been restored — that
> needs a freshly minted PAT.
>
> ⚠️ **Reaching a same-host backend needs the shared `behe-gateway` podman
> network.** A rootless bridged container cannot reach a port published on its
> own host, so co-location makes a backend *harder* to reach, not easier —
> `127.0.0.1`, `host.containers.internal`, the LAN IP and the public vhost all
> refuse, while remote hosts answer 200. Expect older docs to claim the
> opposite.
>
> ⚠️ **An attach failure crash-loops the whole gateway**, taking every other
> surface and `/healthz` with it. Read `podman logs beherouter` after any
> registry change.
>
> 703 tests pass, plus a **local end-to-end stack** (`tests/e2e/`) that proves a
> per-user identity against a REAL `plane-mcp-server`: 22/22 — two callers acting
> as themselves in Plane by PAT, an IdP JWT reaching Plane as `Bearer` through
> `contrib/plane-mcp-bearer`, the stdio `plane` plugin attaching on its default
> `cmd` inside the published image, and `health --deep --bearer-file` proving a
> user's identity end to end. `beheaxi conformance "beherouter"` is 6/6; both backend kinds
> attach for real, and **`cli` backends now execute** (they were listable but
> not callable before 2026-08-04).
> Design background: **`docs/DESIGN.md`**; the plugin seam:
> **`docs/superpowers/specs/2026-09-09-plugins-design.md`**; FastMCP 3.x API notes:
> **`docs/FASTMCP-NOTES.md`**; the 2026-07-30 audit and its four fixes:
> **`docs/superpowers/specs/2026-07-30-beherouter-cleanup-design.md`**.

## Harness standard (read before any interface work)

This repo is the **connectivity keystone** of the BEHEMOTION harness — the
contract it enforces on siblings is defined at the umbrella level. The umbrella
is **not published**, so the two documents below are named rather than linked:
they resolve only in a checkout that has this repo as a child of `BEHEMOTION/`.

- `BEHEMOTION/docs/CONVENTIONS.md` — normative interface
  conventions the gateway assumes (beheaxi manifest = the closed attach
  contract; `<slug>_<verb>` flat tool names; bearer auth; localhost-bound
  backends with the gateway as the only MCP network surface).
- `BEHEMOTION/docs/HARNESS-PLAN.md` — the phased harness
  plan. This repo's items: **Phase 2, items 7–8** (build per the 14-task plan;
  add a backend health contract). Note the attachability reality check there:
  at launch, only `gitea` + `behecheck` can mount.
- [`HARNESS-DIVERGENCES.md`](HARNESS-DIVERGENCES.md) — this repo's audited
  gaps (2026-07-03). Fix one → remove its entry; find a new one → add it there.

Paths assume this repo is checked out as a child of the BEHEMOTION umbrella.

## Mission

beherouter has two faces, and every design decision serves one of them:

> **To agents:** a thin, curated MCP surface — a short pinned tool list plus fuzzy
> search (`search_tools` / `describe_tool` / `run_tool` / `context_cost`) that locates
> the long tail on demand, so a client pays a handful of tool definitions instead of
> hundreds.
>
> **To backends:** pluggable, pre-configured MCP servers — every backend is a
> versioned, tested plugin carrying its own pins, probe, config schema, credential
> names and quirk workarounds, so attaching one is a name in `registry.toml`, not
> an act of archaeology.

The first face has existed since 2026-07-28. The second landed 2026-09-09 as
**Plugins Phase 1** (`docs/superpowers/specs/2026-09-09-plugins-design.md`). The
context-control mechanism is unchanged and still lexical (BM25, *no embeddings*).

**Why "router" and not "mcp":** the layer is growing a second mode — a CLI
router alongside the MCP gateway. That capability is *not yet designed or
built*; it has its own brainstorm → spec → plan cycle. The 2026-08-02 rename
changed the name only, deliberately ahead of the shape, because renaming while
zero surfaces are attached and no consumer is wired is the cheapest this will
ever be. See `docs/superpowers/specs/2026-08-02-beherouter-rename-design.md`.

## The one-paragraph architecture (Approach B)

Build on **FastMCP** (Python). The gateway
is an MCP **server** to clients and an MCP **client** to backends. Each backend is `mount()`ed
/ proxied under its own endpoint (`/<service>/mcp`). Per surface we expose: a small configurable
**pinned** flat-tool set (the common verbs) **+** FastMCP's native **BM25 tool search**
(`BM25SearchTransform` / `RegexSearchTransform`) as
`search_tools`/`describe_tool`/`run_tool`/`context_cost` for everything else and for the
surface's own context cost. Per-user credentials are forwarded per-session (FastMCP forwards arbitrary
headers — so a backend needing two or more per-user headers is handled). Plugin-driven: adding a
service = **a plugin** (an inert `PluginSpec` plus an `async build`) and one line in
`registry.toml`, **not** a new bespoke wrapper file and **not** operator knowledge that
lives only in a comment.

## Why build (not adopt) — settled by a spike

We surveyed the aggregator landscape and **spiked LiteLLM-MCP** (already running on gpu-service).
It was rejected on the decisive axis: LiteLLM's only embeddings-free tool filter is a **static
allowlist** (≡ what we already have), and its **dynamic** search **requires embeddings** (removed
from this homelab). It also forwards only **one** per-user header per backend, and a backend may
well need two (a PAT *plus* a workspace/tenant header is a common shape).
FastMCP gives embeddings-free BM25 search + multi-header passthrough + a place to keep a backend's
own middleware. Full scorecard + landscape in `docs/DESIGN.md`.

## Hard constraints (must preserve)

- **No embeddings.** Tool search must be lexical (BM25/regex). Scale is ~100 tools *per surface*,
  where BM25 is accurate enough.
- **A backend may need more than one per-user header.** A PAT plus a workspace/tenant header is a
  common shape, and it is what ruled out the aggregators that forward exactly one. Per-session
  header passthrough must stay arbitrary-width.
- **Per-backend middleware must stay possible.** A wrapper in front of a backend can carry real
  logic — multi-step upload flows folded into single tools, arg-sanitizing 400-fixers for small
  models. The gateway must be able to **mount such a wrapper as an HTTP backend** rather than force
  a reimplementation.
- **LibreChat quirks** (it's a primary consumer): no native tool-search (so search MUST be real
  MCP tools); and it mis-reads a 401 as "OAuth required" → keep the "anonymous probe → 200, inject
  placeholder Authorization" trick. Also list each surface host in
  `mcpSettings.allowedDomains`.

## Plugins

**A plugin is the only way to attach a backend.** `kind`, `url`, `cmd` and
`transport` no longer exist in `registry.toml`; an entry is a plugin name plus
overrides. The whole live registry is nine lines:

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

⚠️ **That live variable name is the colliding one, and it is deliberate here
only because it is what the vault renders today.** `BEHEROUTER_<SURFACE>_TOKEN`
is the name `beherouter client-config` gives the **client's gateway bearer**;
the backend credential's name is `plugin-config`'s
`BEHEROUTER_PLANE_API_KEY`. Two unrelated secrets under one name, and
cross-wiring them yields a 401 with nothing to point at. `registry-lint` now
says so in its `warnings` array rather than refusing — rotating the live name is
a homelab-repo change (`beherouter-env.j2` + the vault key + this block, in one
playbook run). Every example in this repo uses `BEHEROUTER_PLANE_API_KEY`.

A plugin registers frozen, inert data (`PluginSpec`: pins, probe, config schema,
credential names, backing) plus one `async build(ctx) -> Backend`. Keeping the
declaration inert is load-bearing: it is what lets `plugin-config`,
`registry-lint` and `plugins` read a plugin **without attaching anything**, and
it makes "attach performs no network I/O" mechanically enforceable — a plugin
*cannot* phone home from its spec, only from `build`.

**Four backings:** `http` and `stdio` (an MCP server, over HTTP or as a
subprocess), `cli` (a beheaxi CLI), `native` (in-process Python; Phase 2).

**A plugin no longer has to live in this tree.** A distribution advertising the
`beherouter.plugins` entry-point group is imported at startup and registers the
same way — so a team with a backend of their own extends the gateway instead of
forking it. An entry point that fails to import is logged and skipped (one
third-party package must not cost every other plugin); a registry naming it then
fails loudly at `registry-lint`, and an external plugin can never shadow an
in-tree name. See `docs/PLUGINS.md` § Out-of-tree plugins.

**A backend's searchable catalogue refreshes on a TTL** (`catalogue_ttl_ms`, a
registry-entry override of a `PluginSpec` default; 300 000 ms for both live
surfaces) — `search_tools`/`describe_tool`/`run_tool`/`context_cost` see a
backend's current tools, a failed or slow re-list degrades to serving last-good
and reporting `stale`, and a warm stdio re-list measures in single-digit
milliseconds (see `HARNESS-DIVERGENCES.md`). The *published* `tools` array a
host sees at connect time is frozen for the process lifetime regardless — it is
captured once, at attach, from the pinned set only — so a host's prompt cache is
never invalidated by a catalogue refresh. That freeze is the design's whole
point, not an incidental detail.

⚠️ **`pinned` and `probe` in an entry are OVERRIDES of a tested default, not
required knowledge.** Forgetting them yields the plugin's verified behaviour
rather than an unprobed surface — the `gitea-home` mistake, made unmakeable.
They resolve as one unit: an entry that names its own `probe` supplies its own
`probe_args` too, so an override never inherits another tool's arguments.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty
`${VAR}` is a `UsageError` raised during `build_surfaces` — i.e. at startup — so
it does not yield a broken surface, it yields a **dead gateway**, `/healthz`
included. Entry and secret ship in the same playbook run, or not at all.

**The four commands:**

| Command | Does |
|---|---|
| `beherouter plugins` | what is available, with backing and summary |
| `beherouter plugin-config <surface> <plugin>` | emits all three plumbing fragments — registry block, Caddy `not` clause, `beherouter-env.j2` line — from one spec, so they cannot disagree. Never emits a credential, only a placeholder |
| `beherouter registry-lint [--path P]` | validates a registry offline: no network, no attach. Turns a production-outage-shaped feedback loop into a local one |
| `beherouter context-cost [--surface S] [--context-window N]` | attaches each backend and reports what its published tools cost a client's context; reports per surface rather than raising, so one dead backend does not hide the rest |

### The live surfaces

| Surface | Plugin | Backing | Pinned / advertised | Backend credential |
|---------|--------|---------|--------------------|--------------------|
| **`office`** | `office-mcp` | `http` | 4 / 4 | none — office-mcp has no app-level auth |
| **`plane`** | `plane` | `stdio` | 11 / 30 | **yes** — a Plane PAT (`BEHEROUTER_PLANE_TOKEN`) |
| **`sonarqube`** | `sonarqube` | `http` | 5 / 19 | **yes** — a SonarQube **user** token (`squ_…`; an `sqa_` analysis token 403s every read) |
| _`gcal`_ | `gcal` | `native` | 6 / 6 | **yes** — Google OAuth refresh token |
| _`m365`_ | `m365` | `native` | 6 / 6 | **yes** — Microsoft OAuth refresh token |
| _`gitea-home`_ | — | — | — | removed 2026-07-30; needs a freshly minted PAT |

**Plane has three plugins, one vocabulary.** `plane` (stdio) is what the live
surface uses; `plane-http-apikey` and `plane-http` attach the same 11 pins over
HTTP so the surface can be **per-user**, which stdio can never be. They share
`PINNED`/`PROBE` from `plugins/plane.py` rather than copying them.

⚠️ **The two HTTP mounts are not interchangeable, and the difference is the
whole reason there are two plugins** (measured against plane-mcp-server 0.3.2,
2026-09-22, in `tests/e2e/`):

| Mount | Plugin | What it accepts | Verdict |
|---|---|---|---|
| `/http/api-key/mcp` | `plane-http-apikey` | a per-request **Plane PAT** + `x-workspace-slug` | **works today**: `pat-alice` → Plane resolves alice |
| `/http/mcp` | — | only a token **its own OAuth proxy minted** (a FastMCP JWT with a `jti` in its store) | a forwarded IdP token is **401'd before Plane is consulted**; `plane-http` now **refuses this path** at lint |
| `/bearer/mcp` on [`contrib/plane-mcp-bearer`](contrib/plane-mcp-bearer/README.md) | `plane-http` | a PAT-shaped token → `X-Api-Key`; **anything else → Plane as `Authorization: Bearer`** | **the IdP-token path** (verified in e2e); Plane itself must verify the token. Pinned to plane-mcp-server 0.3.2 exactly — it relies on upstream's private `auth_method` routing |

So per-user Plane against the published server is the PAT path (mode `client`,
each caller's own PAT, nothing stored by the gateway); the bearer path needs
`contrib/plane-mcp-bearer` in front of it. ⚠️ `plane-http` has **no default
`base_url`** since 2026-09-24: the old default pointed at upstream's OAuth proxy,
and its docstring claimed that mount verified the bearer against Plane — a
production deployment followed both and got a surface that attached green and
401'd every user call.

**`office` is a pass-through, and that is deliberate.** office-mcp already does its
own pinned-few + lexical-search split internally (`discover`/`invoke` over a 34-tool
catalogue), so all four of its tools are pinned rather than re-indexed. The benefit
here is **credential centralisation and one uniform client surface, not context
savings.**

**`plane` is the context-savings case** — 11 pinned out of 30 advertised, the other
19 reachable through `search_tools`/`run_tool`. An agent pays 14 tool definitions
instead of 30.

**Plane's traps now live in `src/beherouter/plugins/plane.py`**, versioned with the
code they explain, and three of them are **enforced** rather than merely documented:
the Django underscore-in-`Host` rule is a config validator; the five Community
Edition 404s (`page`, `work_log`, `milestone`, `workitem_type`, `initiative`) and
the uncallable `get_pql_reference` are tests. Read that module's docstring before
changing a pin.

⚠️ **A pinned tool can half-work.** On CE, `workitem` `list` without
`project_id` 404s and any `pql` 400s (measured on CE v1.4.1 by a production
deployment: twelve 404s in one conversation, reported to the user as
"temporary"). All three Plane plugins now carry `edition = "community"` (the
default): those calls are **refused at the gateway** with a non-transient,
actionable message, and `workitem`'s description says so up front.
`edition = "commercial"` turns both off. The mechanism is generic —
`McpBacking.guard` / `.notes` — see `docs/PLUGINS.md`.

⚠️ **The reusable corollary, still manual:** a backend's catalogue advertises the
**commercial** surface, so "the tool exists" says nothing about whether *this*
deployment serves it. **Probe before pinning, and re-probe the whole pin list after
an upgrade or an edition change.**

**Enforced since 2026-09-10:** `beherouter health --deep` fails a surface whose
pin list names a tool the backend no longer serves (`catalogue:
"pinned_missing"`). The re-probe rule above is now mechanical for the
*existence* half; probing that a served tool still *works* is still what
`probe` is for.

⚠️ **Every write through `plane` is attributed to one Plane identity** (the PAT
minted as `beherouter-mcp`). Making the connection universal did not make it
per-user — and `plane` *cannot* be made per-user as attached: it is a `stdio`
backing, which can never carry a per-request identity (an `http` backing for the
plugin is the prerequisite). A surface **with** a `[surface.identity]` table does
forward the caller's own credential; see **`docs/IDENTITY.md`**. Neither live
surface has one.

⚠️ **An attach failure crash-loops the whole gateway**, taking every other surface
and `/healthz` with it. `registry-lint` is the pre-deploy guard; `podman logs
beherouter` is how you find out which backend did it.

### The calendar surfaces — built, NOT attached

⚠️ **`gcal` and `m365` are implemented and tested but attach nowhere yet.** They
are absent from `registry.toml` on purpose: an entry whose `${VAR}` is unset
raises at startup and kills the whole gateway, so the entries ship in the same
playbook run as their vault secrets or not at all. The blocking step is a
one-time human OAuth consent flow with a browser —
**[`docs/CALENDAR-BOOTSTRAP.md`](docs/CALENDAR-BOOTSTRAP.md)**.

They are the first `native` plugins: in-process Python, no sidecar, no Node in
the image, and no writable state (access tokens are held in memory and never
written to disk). Two plugins share one core at
`src/beherouter/plugins/calendar/` — the credential, the surface path and the
Caddy token fork so a dead Google grant cannot take the Microsoft surface down,
while a test asserts the two expose **byte-identical tool schemas** so an agent's
vocabulary cannot drift between them.

Six tools, all pinned: `list_calendars`, `list_events`, `get_freebusy`,
`create_event`, `update_event`, `delete_event`. There is deliberately no
`get_current_time` — the surface's instructions carry the clock.

Four traps, three of them held by tests rather than prose:

- ⚠️ **Google: the OAuth consent screen must be published to Production.** Left
  in Testing, refresh tokens expire after **7 days** and the surface dies weekly.
- ⚠️ **Microsoft: the token endpoint must be the `consumers` authority.**
  `common` issues refresh tokens rejected at the first refresh — it works for an
  hour, then dies. Held by `test_token_url_uses_the_consumers_authority`.
- ⚠️ **Attach performs no network I/O**, so a revoked grant fails a *probe*
  instead of crash-looping the gateway. Held by
  `test_attach_performs_no_network_io`.
- ⚠️ **Every write is attributed to one calendar identity** — the account that
  consented — *unless* the surface declares `[surface.identity]`. Both plugins
  support mode `lookup` against the identity map (`docs/IDENTITY.md`), which
  gives each caller their own refresh token; with no such table, making a
  surface available to every LibreChat user did not make it per-user, exactly as
  with the Plane PAT.

Timestamps crossing the boundary must be RFC 3339 **with an explicit offset**;
naive input is a `UsageError` naming the rule. Both provider APIs accept a naive
time plus a separate timezone field, so the alternative is a meeting silently
booked at the wrong hour and found by a human days later.

## Consumers (must all work)

LibreChat (web VM, no native tool-search) · Hermes (Telegram) · Mac clients (OpenCode/pi) ·
Claude Code (it *does* have native ToolSearch, harmless overlap).

**Codex is not a consumer** — it is not used in this homelab and is not installed
on the Mac. An earlier version of this line named it; that was wrong, and it kept
generating a "Codex dialect" work item across three handoffs. The consumer set is
exactly the five above, and all five have a verified dialect in
`src/beherouter/clientconfig.py`.

## Deploy target — the service VM (198.51.100.114)

beherouter runs on the **service** VM, rootless Podman + systemd user unit + linger, fronted by
Caddy at `https://beherouter.example.com`, app port **47100**. State is config-file only
(`registry.toml`); no database.

**[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is the deployment authority** — the generic
procedure (config, registry, the `registry-lint` pre-deploy gate, proxy and auth, health,
upgrade ordering, rollback) plus a reference-deployment appendix carrying the failure modes
worth knowing before you hit them. It replaced `deploy.md` on 2026-09-11; the old dev-4 plan
that file carried as an appendix was dead twice over and was not carried across: the
deployment went to `service` via ansible, and dev VMs are **Kubernetes-only** — no
podman/compose/systemd-unit
deploys on `dev-1..6` at all.

A machine-local briefing on the services sharing that host (office-mcp, the draw.io pair,
ExcaliDash) lives in **`local-infra/`** — untracked by design, derived from the homelab repo, so
it may be absent in a fresh clone.

**The deployment lives in the homelab repo, not here:**

    $HOMELAB_REPO/ur/service/beherouter/     # compose, Containerfile, registry.toml, vendored src + beheaxi
    cd $HOMELAB_REPO/ansible
    ansible-playbook playbooks/service.yml --tags beherouter   # image + src + registry + .env
    ansible-playbook playbooks/caddy.yml -l service         # the Caddyfile — SEPARATE playbook
    # cold builds: add -e compose_state=stopped to the first one (the unit's
    # TimeoutStartSec=300 would otherwise SIGKILL the build)

⚠️ **Both playbooks, whenever a surface is added or removed.** `--tags beherouter` does not ship the
Caddyfile. Skip the second and the edge and the gateway disagree: on 2026-07-30 the gateway had
already dropped `gitea-home` while Caddy still honored its token, so the retired token passed the
edge and collected 404s instead of being refused. A stale `not` clause would authorize that old
token against whatever surface later claims the same path.

This repo is upstream; that directory holds a **vendored copy** of `src/` and of beheaxi (beheaxi
is public now, so the no-GitHub-token rationale is historical — Workstream D of the
[public-release plan](docs/superpowers/specs/2026-09-11-public-release-design.md) removes the
duplication). Change code here, then
re-vendor — editing there is overwritten by the next sync, and editing on the host is overwritten
by the next playbook run. Caddy vhost (the per-client token boundary), DNS, vault secrets and the
Prometheus probe all live in the homelab repo too: `$HOMELAB_REPO/ur/service/CLAUDE.md` § beherouter.

**Per-surface plumbing is three edits, not one** — a `registry.toml` block, its `not` clause in
`ur/service/Caddyfile`, and its token line in `beherouter-env.j2`. Registry entry alone → the surface
is unreachable (default-deny). Registry + env without the Caddy clause → it answers to another
client's token. **`beherouter plugin-config <surface> <plugin>` emits all three** from the
plugin's own spec, so they cannot disagree; `registry-lint` checks the result before a deploy.

LibreChat wiring stays per-consumer: an `mcpServers` entry + the surface host in
`mcpSettings.allowedDomains`. **The two live surfaces there carry the token differently**
— `office` via per-user `customUserVars` (each user pastes it), `plane` as a
**deploy-time literal** rendered from the vault. See § Next steps 2.

**Kubernetes (the dev-VM path):** dev VMs in this harness are Kubernetes-only, and the repo ships a
Helm chart for exactly that — [`charts/beherouter`](charts/beherouter), with the pre-deploy
`registry-lint` gate wired up as a pre-install/pre-upgrade hook Job and the deployment's failure
modes translated (`maxUnavailable: 0` keeps the previous revision serving). The service-VM
deployment above stays ansible-driven from the homelab repo; the chart is the Kubernetes path.
See `docs/DEPLOYMENT.md` § Deploying on Kubernetes and the chart README.

**The published image** runs as **UID 1000** and ships **`plane-mcp-server`
0.3.2** at `/opt/plane-mcp` (the `plane` plugin's default `cmd`), since
2026-09-24. A stdio command missing from the image is refused at attach **by
name**, and `registry-lint` warns about it. ⚠️ The homelab deployment builds from
its **own** vendored Containerfile, so neither change reaches it until someone
ports it there, and a bind-mounted file the gateway reads must then be readable
by UID 1000.

**Operator signals added 2026-09-24** (from a production deployment's asks):
`GET /metrics` (unauthenticated, counters only) exposes
`beherouter_auth_rejections_total{reason}`. An expired JWT logs at WARNING and
its 401 carries RFC 6750 `error_description="token expired"`.
`beherouter health --deep --bearer-file -` runs each probe **as a user** and
reports the identity the backend returned. `[surface.authz] audience` narrows a
surface to tokens addressed to it, and `BEHEROUTER_OIDC_AUDIENCE` may be a list.
The chart gained `caBundle`, `plugins.install` and `extraInitContainers` /
`extraVolumes` / `extraVolumeMounts`. Details: `docs/IDENTITY.md` §6 and §8,
`docs/DEPLOYMENT.md`.

**Releases are tag-driven and published:** pushing a `vX.Y.Z` tag runs
`.github/workflows/release.yml` — full test suite on the tagged revision, a
tag==pyproject-version==chart-appVersion consistency gate, then the multi-arch
image to **`ghcr.io/behemotion/beherouter`** (image tag = the version, no
leading `v`; that tag is also the chart's default image) and the GitHub
Release, and finally the **chart** as an OCI artifact to
`ghcr.io/behemotion/charts/beherouter` (a published chart version is never
overwritten — the job refuses a `Chart.yaml` `version` that already exists, so
any template or values change bumps it). The workflow must already be on main
when the tag is cut — it runs from the tagged commit. The chart defaults to the
published image, so a client's only required value is `secret.gatewayToken`,
and a Kubernetes user installs with
`helm install beherouter oci://ghcr.io/behemotion/charts/beherouter` instead of
vendoring the chart directory.

**Versioning — patch by default, minor only when asked.** A routine release
bumps the **third** register (`0.2.0 → 0.2.1`); the **second** register moves
`0.2.x → 0.3.0` **only on an explicit user ask** (same for the first). The
bump is synchronized edits to `pyproject.toml` `version` **and** the chart's
`appVersion` (release.yml refuses a tag disagreeing with either); the chart's
own packaging `version` follows the same rule. Stated at each edit point too
(`pyproject.toml`, `charts/beherouter/Chart.yaml`).

## Credentials

No plaintext secrets live in this repo, and nothing current is supplied to it via environment
variables:

- Deployment secrets are **not** env vars here: the gateway token and any per-backend credential
  live in the homelab ansible vault, rendered into `~/beherouter/.env` on the service VM. The
  operator ledger is `$HOMELAB_REPO/secrets/<credential-ledger>`.
- `$DEV4_SSH_PASS` — only for the unexecuted dev-4 plan; nothing current needs it.

## Conventions (inherited from the BEHEMOTION harness)

- **Python via `uv`** (`pyproject.toml` + `uv.lock`). FastMCP, Python ≥3.12.
- **Podman, never Docker.** Auto-start via systemd user unit + linger (see `docs/DEPLOYMENT.md`).
- Keep the gateway **plugin-driven**: adding a service = **a plugin** (a
  `PluginSpec` plus an `async build`) and one line in `registry.toml` to attach
  it — never a bespoke wrapper file, and never operator knowledge that only
  exists in a comment.

## Next steps

The gateway is built, live, and fronting two real backends. What remains is more
backends and the last consumer step, not gateway work.

1. ~~**Attach a real backend.**~~ Done 2026-08-04: `office`. The recipe is a
   `registry.toml` block + a Caddy `not` clause + a vault token, with `probe`
   set from the start. Verify with
   `podman exec beherouter beherouter health --deep --json` — `probe: "ok"` is
   the check `gitea-home` did not have. An env line is needed **only** when the
   backend itself carries a credential.
2. **Wire the consumers.** ~~Mostly done 2026-08-04.~~ `beherouter client-config
   <agent>` emits the block to paste; it never emits a credential, only a
   placeholder. **Each client speaks a different dialect** — a single shape
   cannot satisfy all five, and the generator now emits five (see the table in
   `src/beherouter/clientconfig.py`). The failure modes are asymmetric: OpenCode
   rejects the wrong key loudly, while LibreChat *and* Hermes accept a `${VAR}`
   header they cannot resolve and forward it verbatim, so the only symptom is a
   401 that looks like a bad token.
   - **hermes — verified 2026-08-08** by a real one-shot agent turn calling
     `search_tools("discovr")` through the gateway and getting back the fuzzy
     tier (`discover`, `invoke`). Its key is snake_case `mcp_servers` in
     `~/.hermes/config.yaml` and it has **no** `type` field — the transport is
     inferred from `url`. Tested in an isolated `HERMES_HOME` on the hermes VM;
     the live Telegram bot's config was not touched. Do **not** provision with
     `hermes mcp add` — it derives its own `MCP_<NAME>_API_KEY` and writes the
     token into the profile's `.env`, forking the harness naming convention.
   - **claude-code, pi, opencode — verified** by an agent calling a tool through
     the gateway (`office_search_tools`, the fuzzy tier, which only beherouter
     has — the direct office-mcp server does not, so it cannot be mistaken for it).
     All three are wired on the Mac; the token lives in `~/.secrets.zsh` (0600,
     sourced from `.zshenv`), never in a config file.
   - **LibreChat — wired and deployed for both surfaces; the `office` tool call is
     still NOT verified.** It reaches office *only* through the gateway (the direct
     entry was replaced). `office` uses `customUserVars`, so LibreChat cannot connect
     until a user pastes `BEHEROUTER_OFFICE_TOKEN` in the UI — that last step still
     needs a human.
   - **`plane` in LibreChat bakes the token in at deploy time (2026-09-09)** — the
     opposite choice from `office`, made for two reasons. `packages/data-provider/src/mcp.ts`
     types `StreamableHTTPOptionsSchema.headers` as a plain `z.record(z.string(),
     z.string())` with **no `extractEnvVariable` transform**, so a `${VAR}` placeholder
     is forwarded verbatim and Caddy 401s it; and a literal makes the server work for
     **every** user with no per-user setup, which `customUserVars` cannot do. It also
     closes a rotation-drift hole: rotating the vault token + re-running the playbook
     now updates every user at once, where before it silently 401'd everyone until each
     re-pasted. `babylon/web/librechat.yaml` is rendered with ansible's delimiters
     changed to `[[ ]]` precisely so LibreChat's own `{{ }}` syntax survives.
   - ⚠️ **`requiresOAuth: false` is mandatory for LibreChat** and must appear
     **exactly once** in the entry. Caddy hard-401s every surface with no OAuth
     metadata; without the override LibreChat shows a "Needs Auth" badge whose
     button 404s. A *duplicated* key is worse: LibreChat's YAML parser rejects
     the whole file and silently runs on defaults, dropping every MCP server.
   - ⚠️ **This Mac cannot reach the LAN from Node/Bun/Python** — only from system
     `curl`. Node and Bun get `EHOSTUNREACH`, Python `No route to host`, for
     *every* 198.51.100.x host (LiteLLM and memory-mcp too), while public
     traffic is fine. It is a workstation-level egress gate, **not** a gateway
     fault; don't debug beherouter over it. Verify the gateway with `curl` and
     with `health --deep --json` from inside the VM.
3. **More backends.** ~~Second backend.~~ Done 2026-09-08: `plane` (see § Attached
   surfaces). Re-attaching `gitea-home` needs a freshly minted PAT (the old one was
   revoked and its vault key deleted). Any *same-host* backend must join the
   `behe-gateway` network and allow the gateway's `Host` header — and, if it is a
   Django app, be addressed by a **network alias without underscores**.
4. **Attachability reality check** — see `HARNESS-DIVERGENCES.md` §5 before promising a sibling
   can be fronted: behemem/behetask have nothing to point at yet, and behelib's hand-rolled
   JSON-RPC should wait for its beheaxi migration.

Before interface work, read `HARNESS-DIVERGENCES.md` — it is the live list of what is open here.

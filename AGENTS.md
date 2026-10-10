# beherouter — multi-service MCP gateway

> **Status: a standalone, published product.** Released as **0.2.5**
> (2026-10-06): the image at `ghcr.io/behemotion/beherouter`, the Helm chart at
> `oci://ghcr.io/behemotion/charts/beherouter`, both cut by the tag-driven
> `release.yml`. A large batch is **unreleased on `main`** — generic plugins,
> token exchange, calendar consent, MCP Registry import/export, maturity tiers,
> scheduled deep health, `scripts/deploy.sh`, concurrent attach; see
> `CHANGELOG.md` § Unreleased. There is **no first-party deployment**: the
> homelab reference deployment was retired on 2026-10-07 (§ History). A
> deployer runs it with `scripts/deploy.sh` on a podman host or with the chart
> on Kubernetes — **`docs/DEPLOYMENT.md`** is the authority.
>
> The proof lives in the tree, not in a live host: the test suite (CI: ruff,
> mypy, coverage 100 % (line and branch), pip-audit, hadolint, the version gates), and a
> **local end-to-end stack** (`tests/e2e/`, a CI job) that proves a per-user
> identity against a REAL `plane-mcp-server` — two callers acting as
> themselves in Plane by PAT, an IdP JWT reaching Plane as `Bearer` through
> `contrib/plane-mcp-bearer`, the stdio `plane` plugin attaching on its default
> `cmd` inside the published image, `health --deep --bearer-file` proving a
> user's identity, and `search_tools("sprint")` finding Plane's `cycle`.
> `beheaxi conformance "beherouter"` is 6/6. What each plugin has proven is
> its **maturity tier** (`beherouter plugins`; § Plugins).
>
> ⚠️ **One backend can't take the gateway down** (since 0.2.5). A surface that
> fails to attach, or exceeds `BEHEROUTER_ATTACH_TIMEOUT_S` (30 s), answers
> RFC 9457 `503` and is retried in the background; `/healthz` stays 200 with
> `"status": "degraded"` and a `failed` list. **On `main`:** a configuration
> fault found at attach (`UsageError`) is not retried and is listed under
> `needs_config_change`. Registry mistakes `registry-lint` can see still refuse
> boot. Anything matching `"status":"ok"` (`helm test`, a blackbox probe) fails
> on a degraded gateway — intended.
>
> **Also on `main` (unreleased): hot reload, the kill switch and the admin API**
> (client asks A4 and A11; spec
> `docs/superpowers/specs/2026-10-09-hot-reload-and-kill-switch-design.md`). A
> reload re-attaches only changed surfaces; `${file:/path}` registry values carry
> rotated secrets; `BEHEROUTER_KILLSWITCH_PATH` stops everything, one surface or
> one caller `sub` on the next call. All opt-in; **`admin`, `healthz` and
> `metrics` are now reserved surface names** and an unknown path answers an
> RFC 9457 404. Operator view: **`docs/DEPLOYMENT.md`** § Hot reload, § Kill
> switch, § Admin API. **Stateless surfaces** (client ask A3): `stateless = true` on an
> entry serves it with no MCP session — no session lost at a rollout or reload, no affinity
> needed for that surface (scaling out without affinity needs every surface stateless); refused beside `confirm_mutating` (`docs/DEPLOYMENT.md` § Stateless sessions).
>
> Design background: **`docs/DESIGN.md`**; the plugin seam:
> **`docs/superpowers/specs/2026-09-09-plugins-design.md`** and
> **`docs/PLUGINS.md`**; FastMCP 3.x API notes: **`docs/FASTMCP-NOTES.md`**;
> per-user identity (the corporate half of the pitch): **`docs/IDENTITY.md`**.

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
  add a backend health contract), both done. Since 2026-10-07 the generic
  plugins can mount behemem, behecheck and behesid; see
  `HARNESS-DIVERGENCES.md` §5.
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

**The problem it solves has two halves, and the README leads with both.** Every
advertised tool definition costs context, which the pinned + search split handles.
Every call through a shared service credential is made **as one account**, so the
backend's permissions don't apply and the audit trail says "the bot did it". The
identity work (2026-09-21 onward, `docs/IDENTITY.md`) handles that: the gateway
**verifies the caller** (an OIDC JWT, beside or instead of the shared token) and
**forwards that caller's identity** to each backend in the form it expects. That
second half is what makes the gateway usable in a company rather than only for
one person, so treat it as part of the mission, not an add-on. It is **opt-in per
surface**: with no auth mode and no `[surface.identity]`, behaviour is exactly the
shared-token gateway.

**Why "router" and not "mcp":** the 2026-08-02 rename anticipated a second
mode, a CLI router beside the MCP gateway. **That mode is dropped (decided
2026-10-07, `docs/superpowers/specs/2026-10-07-cli-router-dropped.md`).**
beherouter reaches CLIs only as *backends*, through the `beheaxi-cli` plugin.
No `beherouter call …` consumer mode exists or is planned. The name stays,
because the gateway routes agents to backends. Don't build the router from the
rename spec's wording; reopening it needs a new brainstorm.

## The one-paragraph architecture (Approach B)

Build on **FastMCP** (Python). The gateway
is an MCP **server** to clients and an MCP **client** to backends. Each backend is `mount()`ed
/ proxied under its own endpoint (`/<service>/mcp`). Per surface we expose: a small configurable
**pinned** flat-tool set (the common verbs) **+** beherouter's own **lexical tool index**
(`search.py`: field-weighted BM25 + prefix/fuzzy expansion, no embeddings; FastMCP's
`BM25SearchTransform` was evaluated and rejected — see docs/DESIGN.md § Search design) as
`search_tools`/`describe_tool`/`run_tool`/`context_cost` for everything else and for the
surface's own context cost. Identity is two halves: **in** is gateway-wide
(`BEHEROUTER_AUTH_MODE` = `shared` | `oidc` | `both`, a JWKS-checked JWT), **out** is per
surface (`[surface.identity]` mode `bearer` | `claims` | `client` | `lookup` | `exchange`,
landing as per-call headers for `http`/`inproc`, subprocess env for `cli`, a per-identity
credential provider for `native`; refused for `stdio`; `exchange` header targets only), plus an optional `[surface.authz]` role/audience gate, per-tool role gates, `confirm_mutating` (human confirmation by MCP elicitation) and a per-caller `[surface.rate_limit]`. Header
passthrough is arbitrary-width, so a backend needing two or more per-user headers is handled.
Plugin-driven: adding a
service = **a plugin** (an inert `PluginSpec` plus an `async build`) and one line in
`registry.toml`, **not** a new bespoke wrapper file and **not** operator knowledge that
lives only in a comment.

## Why build (not adopt) — settled by a spike

We surveyed the aggregator landscape and **spiked LiteLLM-MCP** (then running in the homelab).
It was rejected on the decisive axis: LiteLLM's only embeddings-free tool filter is a **static
allowlist** (≡ what we already have), and its **dynamic** search **requires embeddings** (which
this project rules out). It also forwards only **one** per-user header per backend, and a backend may
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
  MCP tools); and it mis-reads a 401 as "OAuth required", so its generated config carries
  `requiresOAuth: false` (§ Consumers). Also list each surface host in
  `mcpSettings.allowedDomains`.
- **Identity stays opt-in, and a surface is per-user or it isn't.** No auth mode and no
  `[surface.identity]` must keep meaning the pre-identity gateway. Don't add an
  "optional identity" boolean: it invites the state the feature exists to prevent, a
  surface believed per-user and actually shared.
- **`stdio` can never be per-user.** A kept-alive subprocess can't carry per-request
  material. It is refused at lint, at `validate_entry` and in `build_transport`. Never
  loosen one of the three.
- **Attach does no identity resolution, no identity-map read and no token exchange.** All
  happen on the call path, so a bad map or an IdP outage degrades one surface's calls instead
  of failing its attach.
- **Never log or echo an identity value.** Logs carry the subject, the mode and the *names*
  of the applied material; refusals name the claim or expected audience, never the
  caller's value. The same holds for `--textfile` output: labels are the surface and a
  verdict enum, nothing else. The one exception is the audit line (`beherouter.audit`),
  which may carry the claim values an operator names in `BEHEROUTER_AUDIT_CLAIMS` — never a
  token, header, credential or exchange material, and never the call's arguments.

## Plugins

**A plugin is the only way to attach a backend.** `kind`, `url`, `cmd` and
`transport` no longer exist in `registry.toml`; an entry is a plugin name plus
overrides:

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

⚠️ **Never name a backend credential `BEHEROUTER_<SURFACE>_TOKEN`.** That is the
name `beherouter client-config` gives the **client's gateway bearer**; the backend
credential's name is `plugin-config`'s `BEHEROUTER_<SURFACE>_API_KEY`. Two
unrelated secrets under one name, and cross-wiring them yields a 401 with nothing
to point at. `registry-lint` warns (never refuses) on the colliding name; the
retired homelab registry used it for Plane, which is why the warning exists.

A plugin registers frozen, inert data (`PluginSpec`: pins, probe, config schema,
credential names, identity support, search vocabulary, maturity, backing) plus
one `async build(ctx) -> Backend`. Keeping the declaration inert is load-bearing:
it is what lets `plugin-config`, `registry-lint`, `plugins` and `catalog-export`
read a plugin **without attaching anything**, and it makes "attach performs no
network I/O" mechanically enforceable — a plugin *cannot* phone home from its
spec, only from `build`.

**Five backings:** `http` and `stdio` (an MCP server, over HTTP or as a
subprocess), `cli` (a beheaxi CLI), `native` (in-process Python), `inproc` (an
in-process FastMCP server; the `openapi` and `python-dir` sources and decorator
plugins). ⚠️ `python-dir` runs operator code in the gateway process; lint only
parses it, and its sibling imports must be at module top level (each
directory's modules are private to its surface).

**The plugins in the tree, and what each has proven:**

| Plugin | Backing | Tier | Fronts |
|---|---|---|---|
| `plane-http` | `http` | per-user | Plane over HTTP, the caller's IdP token (through `contrib/plane-mcp-bearer`) |
| `plane` | `stdio` | verified | Plane (`plane-mcp-server` 0.3.2, shipped in the image) — 11 pinned of 30 |
| `plane-http-apikey` | `http` | verified | Plane over HTTP, the caller's own PAT |
| `office-mcp` | `http` | catalogued | office-mcp — 4 pinned of 4 |
| `sonarqube` | `http` | catalogued | SonarQube — 5 pinned (18 tools in the recorded 1.27.0.4335 catalogue); needs a **user** token (`squ_…`; an `sqa_` analysis token 403s every read) |
| `gcal`, `m365` | `native` | catalogued | Google / Microsoft 365 calendars — 6 of 6 |
| `mcp-http`, `mcp-stdio`, `beheaxi-cli` | `http`, `stdio`, `cli` | declared | generic, by URL / command / beheaxi CLI |
| `openapi`, `python-dir` | `inproc` | declared | generic, a REST API / a directory of `@tool` functions |

**Maturity** is `declared` < `probed` < `catalogued` < `verified` < `per-user`: a
claim about **evidence**, declared on the spec (`maturity=`, `evidence=`) and held
by `tests/test_maturity.py` through `beherouter.testing.plugin_conformance`.
`registry-lint` warns on a `declared` plugin; an out-of-tree plugin is shown as
`probed` at most unless its evidence ships in its own distribution. Raise a tier
together with its evidence, never alone. See `docs/PLUGINS.md` § Maturity.

**Generic plugins** attach a backend with no curated plugin: `mcp-http` (by URL),
`mcp-stdio` (by command), `beheaxi-cli` (any beheaxi CLI — the first production
plugin on the `cli` backing). Each makes `probe` **mandatory** via
`requires_entry` (the MCP two also `pinned`), so the `gitea-home` hole — an
unprobed surface — stays closed. Their credential is optional
(`EnvVar(required=False)`, named `api_key`) and `plugin-config` emits it
commented out. This is what lets the harness's own layers mount: behemem
(`mcp-http`), behecheck (`mcp-stdio`), behesid (`beheaxi-cli`) — example
entries in `docs/PLUGINS.md` § Generic plugins.

**The MCP Registry is an import source, not a plugin.** `catalog-import` turns a
`server.json` into an `mcp-http`/`mcp-stdio` entry with exact version pins;
`pinned`/`probe` come out as TODOs that **fail `registry-lint`** unless the
document carries an advisory `io.beherouter/plugin` `_meta` block.
`catalog-export <plugin>` writes the reverse for a private subregistry. ⚠️ The
published image has no Node and no `uvx` alias, so an imported `npm`/`pypi`
package needs a derived image. See `docs/PLUGINS.md` § Importing from the MCP
Registry.

**A plugin no longer has to live in this tree.** A distribution advertising the
`beherouter.plugins` entry-point group is imported at startup and registers the
same way — so a team with a backend of their own extends the gateway instead of
forking it. An entry point that fails to import is logged, **recorded** and
skipped (one third-party package must not cost every other plugin): `plugins`
lists it under `failed`, `registry-lint` warns, and a registry naming it fails
with an `unknown plugin` error that names the failed entry points. An external
plugin can never shadow an in-tree name. See `docs/PLUGINS.md` § Out-of-tree
plugins.

**A backend's searchable catalogue refreshes on a TTL** (`catalogue_ttl_ms`, a
registry-entry override of a `PluginSpec` default; 300 000 ms) —
`search_tools`/`describe_tool`/`run_tool`/`context_cost` see a backend's current
tools, a failed or slow re-list degrades to serving last-good and reporting
`stale`, and a warm stdio re-list measures in single-digit milliseconds (see
`HARNESS-DIVERGENCES.md`). The *published* `tools` array a host sees at connect
time is frozen for the process lifetime regardless — it is captured once, at
attach, from the pinned set only — so a host's prompt cache is never invalidated
by a catalogue refresh. That freeze is the design's whole point, not an
incidental detail.

**Search vocabulary.** A plugin may declare `search_aliases` — words agents type
that the backend's descriptions lack (Plane: `sprint` → `cycle`). A registry
entry may add words under `[surface.search_aliases]`, never remove them. Quality
is gated by `tests/test_search_eval.py` against recorded catalogues (Plane,
SonarQube, office-mcp); after a backend upgrade, re-record with
`scripts/record_catalogue.py` and re-run that test.

⚠️ **`pinned` and `probe` in an entry are OVERRIDES of a tested default, not
required knowledge** (generic plugins excepted: they have no default, so the
entry must set them). Forgetting them on a curated plugin yields its verified
behaviour rather than an unprobed surface — the `gitea-home` mistake, made
unmakeable. They resolve as one unit (`plugins.resolve_probe`): an entry that
names its own `probe` supplies its own `probe_args` too, so an override never
inherits another tool's arguments.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty
`${VAR}` is a `UsageError` raised during `build_surfaces` — i.e. at startup — so
it does not yield a broken surface, it yields a **dead gateway**, `/healthz`
included. Entry and secret ship in the same deploy, or not at all.

**The commands:**

| Command | Does |
|---|---|
| `beherouter plugins` | what is available: backing, `generic`, maturity tier, summary; plus `failed` entry points |
| `beherouter plugin-config <surface> <plugin>` | emits all three plumbing fragments — registry block, reverse-proxy clause, env line — from one spec, so they cannot disagree. Never emits a credential, only a placeholder |
| `beherouter registry-lint [--path P]` | validates a registry offline: no network, no attach. Refusals plus a `warnings` array (declared plugins, missing `cmd`, colliding variable names, unset exchange secrets, failed entry points) |
| `beherouter attach <surface> <plugin> [--config k=v,…]` / `detach` | edits `registry.toml` in place, comments kept (`tomlkit`); values coerced (int, float, `true`/`false`), repeat a key for a list |
| `beherouter catalog-import <file\|URL\|name> <surface> [--registry URL]` | registry fragments from an MCP Registry `server.json` |
| `beherouter catalog-export <plugin>` | a curated plugin as `server.json` with its pins and probe under `_meta` |
| `beherouter context-cost [--surface S] [--context-window N]` | attaches each backend and reports what its published tools cost a client's context; per surface rather than raising, so one dead backend does not hide the rest |
| `beherouter health [--deep] [--bearer-file F] [--textfile P]` | `--deep`: a real credentialed probe per surface, concurrently; `--bearer-file`: the same as a user; `--textfile`: the verdict for node_exporter |
| `beherouter calendar-consent <surface> --subject S [--revoke]` | one user's calendar consent into the identity map |
| `beherouter client-config <agent>` | paste-ready config for one of the five consumers |
| `beherouter --version` | the installed version (package metadata, never a literal) |

### Per-user identity

A plugin declares which identity modes it honours (`IdentitySupport` on its
`PluginSpec`), and `registry-lint` refuses any other mode offline. A plugin that
declares nothing can't be configured for per-user use at all. Today:

| Plugin | Modes | What each caller brings |
|---|---|---|
| `office-mcp` | `bearer`, `claims`, `client`, `exchange` | their JWT, asserted claims, named headers, or a token exchanged for them |
| `plane-http-apikey` | `client` | their own Plane PAT; works against stock `plane-mcp-server` |
| `plane-http` | `bearer`, `exchange` | their IdP token (or one exchanged for Plane), through `contrib/plane-mcp-bearer`; Plane must verify it |
| `gcal`, `m365` | `lookup` | nothing; their refresh token is read from the identity map, put there by `calendar-consent` |
| `openapi` | `bearer`, `claims`, `client`, `lookup`, `exchange` | their material, on the upstream REST request |
| `mcp-http` | `bearer`, `claims`, `client`, `exchange` | their material, as headers to any HTTP MCP server |
| `beheaxi-cli` | `claims` | asserted claims, as the verb subprocess's environment |
| `plane`, `sonarqube`, `python-dir`, `mcp-stdio` | — | shared credential only (`plane` and `mcp-stdio` are stdio, so they never can be; `python-dir` functions have nowhere stable to read the caller from yet) |

⚠️ **`exchange` (RFC 8693) never falls back.** The gateway, as its own OAuth
client, trades the caller's verified JWT for a token addressed to the backend and
forwards that as a header; a failed exchange never sends the call, and nothing
— subject token, issued token, client secret or the IdP's `error_description` —
is logged. `client_secret` must be a `${VAR}`, resolved per exchange, so an unset
one degrades that surface's calls rather than the boot (`registry-lint` warns).
Header backings only. Design record:
`docs/superpowers/specs/2026-10-07-token-exchange-design.md`.

Proving it: a green `health --deep` proves only the **deployment** credential (its
output says `probe_scope: "deployment-credential"`).
`health --deep --bearer-file -` repeats the probe **as a user** and reports
`matches_caller`; `mismatch` is the incident this exists to catch. The e2e stack
(`tests/e2e/`) is the reference proof. Refusal rules, the identity map, the gates
and the operator signals: **`docs/IDENTITY.md`**. When the README and this file
describe identity, they describe the same table; update both together.

### Plane — three plugins, one vocabulary

`plane` (stdio) is the simplest attach; `plane-http-apikey` and `plane-http`
attach the same 11 pins over HTTP so the surface can be **per-user**, which stdio
can never be. They share `PINNED`/`PROBE` and the mount constants from
`plugins/plane.py` rather than copying them.

⚠️ **The two HTTP mounts are not interchangeable, and the difference is the
whole reason there are two plugins** (measured against plane-mcp-server 0.3.2,
2026-09-22, in `tests/e2e/`):

| Mount | Plugin | What it accepts | Verdict |
|---|---|---|---|
| `/http/api-key/mcp` | `plane-http-apikey` | a per-request **Plane PAT** + `x-workspace-slug` | **works**: `pat-alice` → Plane resolves alice |
| `/http/mcp` | — | only a token **its own OAuth proxy minted** (a FastMCP JWT with a `jti` in its store) | a forwarded IdP token is **401'd before Plane is consulted**; `plane-http` **refuses this path** at lint |
| `/bearer/mcp` on [`contrib/plane-mcp-bearer`](contrib/plane-mcp-bearer/README.md) | `plane-http` | a PAT-shaped token → `X-Api-Key`; **anything else → Plane as `Authorization: Bearer`** | **the IdP-token path** (verified in e2e); Plane itself must verify the token. Pinned to plane-mcp-server 0.3.2 exactly — it relies on upstream's private `auth_method` routing |

So per-user Plane against the published server is the PAT path (mode `client`,
each caller's own PAT, nothing stored by the gateway); the bearer path needs
`contrib/plane-mcp-bearer` in front of it. ⚠️ `plane-http` has **no default
`base_url`** since 2026-09-24: the old default pointed at upstream's OAuth proxy,
and its docstring claimed that mount verified the bearer against Plane — a
production deployment followed both and got a surface that attached green and
401'd every user call.

⚠️ **Every write through `plane` (stdio) is attributed to one Plane identity**,
the PAT's owner. Making a surface available to every user does not make it
per-user; switching to `plane-http-apikey` (mode `client`) or `plane-http` (mode
`bearer`/`exchange`) does.

**Plane's traps live in `src/beherouter/plugins/plane.py`**, versioned with the
code they explain, and three of them are **enforced** rather than merely
documented: the Django underscore-in-`Host` rule is a config validator; the five
Community Edition 404s (`page`, `work_log`, `milestone`, `workitem_type`,
`initiative`) and the uncallable `get_pql_reference` are tests. Read that
module's docstring before changing a pin.

⚠️ **A pinned tool can half-work.** On CE, `workitem` `list` without
`project_id` 404s and any `pql` 400s (measured on CE v1.4.1 by a production
deployment: twelve 404s in one conversation, reported to the user as
"temporary"). All three Plane plugins carry `edition = "community"` (the
default): those calls are **refused at the gateway** with a non-transient,
actionable message, and `workitem`'s description says so up front.
`edition = "commercial"` turns both off. The mechanism is generic —
`McpBacking.guard` / `.notes` — see `docs/PLUGINS.md`.

### `office-mcp` is a pass-through, deliberately

office-mcp already does its own pinned-few + lexical-search split internally
(`discover`/`invoke` over a 34-tool catalogue), so all four of its tools are
pinned rather than re-indexed. The benefit there is **credential centralisation
and one uniform client surface, not context savings.** `plane` is the
context-savings case — 11 pinned out of 30, the other 19 reachable through
`search_tools`/`run_tool`: an agent pays 14 tool definitions instead of 30.

### Pins: probe before pinning

⚠️ A backend's catalogue advertises the **commercial** surface, so "the tool
exists" says nothing about whether *this* deployment serves it. **Probe before
pinning, and re-probe the whole pin list after an upgrade or an edition change.**
The *existence* half is mechanical: a pinned tool the backend no longer serves
logs a WARNING at attach and shows under `/healthz` `pinned_missing` (the surface
is still published, status unchanged), and `health --deep` fails the surface
(`catalogue: "pinned_missing"`). Proving a served tool still *works* is what
`probe` is for.

### The calendar plugins (`gcal`, `m365`)

The first `native` plugins: in-process Python, no sidecar, no Node in the image,
and no writable state (access tokens are held in memory and never written to
disk). Two plugins share one core at `src/beherouter/plugins/calendar/` — the
credential, the surface path and the proxy token fork so a dead Google grant
cannot take the Microsoft surface down, while a test asserts the two expose
**byte-identical tool schemas** so an agent's vocabulary cannot drift between
them. Bootstrapping a grant is a one-time human OAuth consent with a browser:
**[`docs/CALENDAR-BOOTSTRAP.md`](docs/CALENDAR-BOOTSTRAP.md)**.

Six tools, all pinned: `list_calendars`, `list_events`, `get_freebusy`,
`create_event`, `update_event`, `delete_event`. There is deliberately no
`get_current_time` — the surface's instructions carry the clock.

**Per-user calendars: `beherouter calendar-consent <surface> --subject <claim
value>`** runs the provider's authorization-code flow with PKCE against a
listener on `127.0.0.1`, and writes that subject's refresh token into the
identity map (atomically, mode 0600) for a `lookup` surface; `--revoke` removes
it (and revokes upstream at Google). A CLI verb, not a gateway route: a browser
redirect carries no bearer, and the gateway's only network surface is
bearer-authenticated. Design record:
`docs/superpowers/specs/2026-10-07-calendar-consent-design.md`.

Traps, three of them held by tests rather than prose:

- ⚠️ **Google: the OAuth consent screen must be published to Production.** Left
  in Testing, refresh tokens expire after **7 days** and the surface dies weekly.
- ⚠️ **Microsoft: the token endpoint must be the `consumers` authority.**
  `common` issues refresh tokens rejected at the first refresh — it works for an
  hour, then dies. Held by `test_token_url_uses_the_consumers_authority`.
- ⚠️ **Attach performs no network I/O**, so a revoked grant fails a *probe*
  instead of failing the attach. Held by `test_attach_performs_no_network_io`.
- ⚠️ **Every write is attributed to one calendar identity** — the account that
  consented — *unless* the surface declares `[surface.identity]` mode `lookup`.
- Evicted per-identity providers are closed once no call is using them
  (refcounted); before 2026-10-07 they leaked their HTTP clients.

Timestamps crossing the boundary must be RFC 3339 **with an explicit offset**;
naive input is a `UsageError` naming the rule. Both provider APIs accept a naive
time plus a separate timezone field, so the alternative is a meeting silently
booked at the wrong hour and found by a human days later.

## Consumers (must all work)

LibreChat (no native tool-search) · Hermes · pi · OpenCode · Claude Code (it
*does* have native ToolSearch, harmless overlap).

**Codex is not a consumer.** An earlier version of this line named it; that was
wrong, and it kept generating a "Codex dialect" work item across three handoffs.
The consumer set is exactly the five above, and all five have a dialect in
`src/beherouter/clientconfig.py`, each established by pasting the generated
output unmodified into the client and having an agent call a tool through it
(2026-08, against the homelab deployment).

`beherouter client-config <agent>` emits the block to paste; it never emits a
credential, only a placeholder. **Each client speaks a different dialect** — a
single shape cannot satisfy all five (table in `clientconfig.py`). The failure
modes are asymmetric: OpenCode rejects the wrong key loudly, while LibreChat
*and* Hermes accept a `${VAR}` header they cannot resolve and forward it
verbatim, so the only symptom is a 401 that looks like a bad token.

- **Hermes:** snake_case `mcp_servers` in `~/.hermes/config.yaml`, with **no**
  `type` field — the transport is inferred from `url`. Do **not** provision with
  `hermes mcp add`: it derives its own `MCP_<NAME>_API_KEY` and writes the token
  into the profile's `.env`, forking the naming convention.
- **LibreChat:** a `${VAR}` in `headers` is **not** expanded
  (`StreamableHTTPOptionsSchema.headers` is a plain `z.record` with no
  `extractEnvVariable` transform), so either use `customUserVars` (each user
  pastes their token) or render a literal at deploy time (works for every user,
  and a rotation reaches everyone at once). List each surface host in
  `mcpSettings.allowedDomains`.
- ⚠️ **LibreChat: `requiresOAuth: false` is mandatory and must appear exactly
  once.** A proxy that 401s a surface with no OAuth metadata otherwise makes
  LibreChat show a "Needs Auth" badge whose button 404s. A *duplicated* key is
  worse: LibreChat's YAML parser rejects the whole file and silently runs on
  defaults, dropping every MCP server. `client-config librechat` emits it once.

## Deploying

State is config-file only (`registry.toml`, plus an optional read-only identity
map); no database; app port **47100**; the gateway binds loopback behind a
reverse proxy that is the per-client token boundary.
**[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is the deployment authority**:
config, registry, the `registry-lint` pre-deploy gate, proxy and auth, health,
scheduled deep health, upgrade ordering, rollback, and the failure modes worth
knowing before you hit them.

- **Single podman host: `scripts/deploy.sh`.** Pull (or `--build`) → `registry-lint`
  **inside the new image** with the env it will serve with → snapshot the
  registry → stop and keep the old container as `<name>-previous` → start →
  verify `/healthz` (and `health --deep` with `--deep`) → on any failure, roll
  back to the previous container **and registry snapshot**.
- **Kubernetes: the chart** (`charts/beherouter`, published as
  `oci://ghcr.io/behemotion/charts/beherouter`). The `registry-lint` gate is a
  pre-install/pre-upgrade hook Job; `maxUnavailable: 0` keeps the previous
  revision serving; `caBundle`, `plugins.install` (plus `plugins.indexes[]` and
  `plugins.local` wheels from a ConfigMap, on `main`), `extraInitContainers` /
  `extraVolumes` / `extraVolumeMounts`; and since chart 0.1.7 an opt-in
  `healthCronJob` (requires `healthCronJob.textfile.hostPath`). Hook and test
  pods deliberately do **not** carry the gateway's selector labels, so the
  Service, PDB and NetworkPolicy never count them as replicas.

**Per-surface plumbing is three edits, not one** — a `registry.toml` block, its
reverse-proxy allow clause, and its token/credential env line. Registry entry
alone → the surface is unreachable (default-deny). Registry + env without the
proxy clause → it answers to another client's token. A stale proxy clause is
worse than a missing one: it authorizes a retired token against whatever surface
later claims the path. **`beherouter plugin-config <surface> <plugin>` emits all
three** from the plugin's own spec; `registry-lint` checks the result.

**The published image** runs as **UID 1000**, builds from the committed `uv.lock`
(`uv sync --frozen`, `uv` pinned by version and digest), and ships
**`plane-mcp-server` 0.3.2** at `/opt/plane-mcp` (the `plane` plugin's default
`cmd`). A stdio command missing from the image is refused at attach **by name**,
and `registry-lint` warns about it (for `cli` plugins too). A bind-mounted file
the gateway reads must be readable by UID 1000.

**Admin routes** (off unless `BEHEROUTER_ADMIN_TOKEN` or `BEHEROUTER_ADMIN_ROLE` is
set; no credential = no `/admin` route, 404; the kill-switch ones also need
`BEHEROUTER_KILLSWITCH_PATH`). ⚠️ Allow `/admin` at the proxy from the admin network
only; the admin token must differ from the gateway token (boot refuses).

| Route | Does |
|---|---|
| `POST /admin/reload` | reload the registry; 200 result, 422 when lint failed, 503 after shutdown began |
| `GET /admin/killswitch` | the state (audited as `read_killswitch`) |
| `PUT` / `DELETE /admin/killswitch/all` | stop / resume every surface |
| `PUT` / `DELETE /admin/killswitch/surfaces/{name}` | quarantine / release one surface |
| `POST /admin/killswitch/subjects/block` / `unblock` | block / unblock a caller; `{"sub"}` in the **body**, never the path |

Other reload triggers: `kill -HUP`, `BEHEROUTER_REGISTRY_WATCH_S`. Drain:
`BEHEROUTER_RELOAD_DRAIN_S` (30). The rate limiter carries over per surface on an equal
`[rate_limit]`. A changed surface whose new app fails to attach **or start** keeps
serving its old app (`reload_failed`). Gateway-wide environment is restart-only.

**Operator signals.** `/healthz` (unauthenticated): `status` `ok`|`degraded`,
`surfaces`, and when they apply `failed`, `needs_config_change` (failed surfaces
no longer retried — a configuration fault), `pinned_missing`
(`{surface: [tools]}`), `reload_failed` (still serving the pre-reload app),
`last_reload` (`{status: failed, at}` while the last reload failed lint), `disabled`
(quarantined names, `["*"]` for `all`) and `killswitch: "stale"` (malformed state
file, last good in force). The kill switch never degrades `status`: alert on
`disabled`. New series: `beherouter_reloads_total{trigger,outcome}`,
`beherouter_reload_last_success_timestamp_seconds`,
`beherouter_surface_disabled{surface}`, `beherouter_blocked_subjects`. Admin requests
write an `event: admin` audit line (a subject's `target` as `sha256:<8 hex>`; `actor` is the admin's `sub`, `<admin-token>`, `<unknown-subject>` for a JWT without one, or `<refused>` for a refused request). `GET /metrics` (unauthenticated): the five original series
`beherouter_tool_calls_total{surface,tool,outcome}`,
`beherouter_tool_call_duration_seconds{surface,tool}`,
`beherouter_active_sessions{surface}`, `beherouter_surface_up{surface}` and
`beherouter_auth_rejections_total{reason,surface}` (`surface` is `""` when the path
names no configured surface). Counters reset on restart: use `rate()`/`increase()`.
`beherouter_active_sessions` reads a private FastMCP attribute and is omitted, with a
WARNING, if FastMCP stops exposing it. Every call writes one JSON **audit line** on
`beherouter.audit` (stdout; `BEHEROUTER_AUDIT=off` disables it; extra claims via
`BEHEROUTER_AUDIT_CLAIMS`; never arguments). `BEHEROUTER_LOG_FORMAT=text|json` shapes
the main log. `call_timeout_s` (registry entry) or `BEHEROUTER_CALL_TIMEOUT_S` bounds a
backend call, unset = no limit. A failed call returns `isError: true` with the human
text and `_meta["io.beherouter/error"] = {type, code, reason, context}`; `reason` is one
of `unauthenticated`, `missing_role`, `wrong_audience`, `identity_unavailable`,
`rate_limited`, `confirmation_required`, `surface_disabled`, `caller_blocked`, `unknown_tool`, `bad_arguments`,
`edition_unsupported`, `backend_rejected`, `backend_unavailable`, `timeout`, `internal` (`errors.REASONS`; `context` names what was
required, never what the caller had). Details: `docs/DEPLOYMENT.md` § Logs, audit and
metrics. An expired JWT logs at WARNING and
its 401 carries RFC 6750 `error_description="token expired"`. **`/healthz`
cannot see a revoked credential**, so schedule `health --deep --textfile PATH`
(add `--bearer-file` for the per-user verdict): `contrib/health-textfile/` has
the metric contract, a systemd timer, a CronJob and alert rules (staleness
included — a sweep that cannot run writes nothing). Details:
`docs/IDENTITY.md` §6 and §8, `docs/DEPLOYMENT.md`.

**Releases are tag-driven and published:** pushing a `vX.Y.Z` tag runs
`.github/workflows/release.yml` — the full gate set on the tagged revision
(ruff, mypy, tests with the coverage floor, pip-audit), a
tag == `pyproject` version == chart `appVersion` == `beherouter --version`
consistency gate, then the multi-arch image to **`ghcr.io/behemotion/beherouter`**
(image tag = the version, no leading `v`; also the chart's default image), the
GitHub Release, and finally the **chart** as an OCI artifact to
`ghcr.io/behemotion/charts/beherouter` (a published chart version is never
overwritten — the job refuses a `Chart.yaml` `version` that already exists, so
any template or values change bumps it). **A release also writes its
`CHANGELOG.md` section**: the workflow refuses to publish without a `##
[X.Y.Z]` heading and uses that section as the release body. Rename `##
[Unreleased]` to the version at release time. The workflow must already be on
main when the tag is cut — it runs from the tagged commit. Every action is
pinned to a commit SHA (Dependabot bumps them).

The `/deploy` skill (`.claude/skills/deploy/SKILL.md`, untracked) covers the
public release only: green main, version consistency, the release commit, the
tag, artifact verification. It no longer carries homelab steps — there is no
first-party deployment to push a release to; a deployer follows
`docs/DEPLOYMENT.md`.

**Versioning — patch by default, minor only when asked.** A routine release
bumps the **third** register (`0.2.0 → 0.2.1`); the **second** register moves
`0.2.x → 0.3.0` **only on an explicit user ask** (same for the first). The
bump is synchronized edits to `pyproject.toml` `version` **and** the chart's
`appVersion` (CI and release.yml refuse a disagreement); the chart's own
packaging `version` follows the same rule. Stated at each edit point too
(`pyproject.toml`, `charts/beherouter/Chart.yaml`).

## Credentials

No plaintext secrets live in this repo. Backend credentials and the gateway
token belong to the deployer's secret store, reach the gateway as environment
variables (`scripts/deploy.sh --env-file`, the chart's `secret.*`), and appear in
`registry.toml` only as `${VAR}` placeholders. Nothing in this repo's own
workflow needs a credential; the e2e stack mints throwaway key material
(`tests/e2e/mint.py`).

## Conventions (inherited from the BEHEMOTION harness)

- **Python via `uv`** (`pyproject.toml` + `uv.lock`, `.python-version` 3.12).
  FastMCP `>=3.4,<4`, Python ≥3.12.
- **Gates before a change is done:** `uv run ruff check src tests`,
  `uv run mypy src`, `uv run pytest -q --cov=beherouter` (floor 100 %, line and branch, in
  `pyproject.toml` — raise it, never lower it). `ruff format` is deliberately
  not enforced.
- **Podman, never Docker**, for anything this repo runs (`scripts/deploy.sh`,
  `podman-compose.yml`, the e2e stack). `.dockerignore` exists only for
  BuildKit and must stay identical to `.containerignore` (CI diffs them).
- Keep the gateway **plugin-driven**: adding a service = **a plugin** (a
  `PluginSpec` plus an `async build`) and one entry in `registry.toml` to attach
  it — never a bespoke wrapper file, and never operator knowledge that only
  exists in a comment. No curated plugin yet? A generic one plus a `probe`.

## What's next

The gateway is built and published; what remains is shipping what is on `main`
and widening what is proven.

1. **Cut the next release** (a patch, 0.2.6, unless a minor is asked for):
   `CHANGELOG.md` § Unreleased is written. Run the e2e stack first (CI's `e2e`
   job, or `tests/e2e/up.sh` locally): its `registry-lint` check was updated to
   expect two warnings.
2. **Raise tiers with evidence.** `office-mcp` has no e2e check (`catalogued`);
   `plane-http-apikey` needs a check asserting `matches_caller` for mode `client`
   to reach `per-user`.
3. **Mount the harness layers** wherever a deployment wants them — behemem
   (`mcp-http`), behecheck (`mcp-stdio`), behesid (`beheaxi-cli`) — and graduate
   a generic entry to a curated plugin once its catalogue warrants versioned pins.
   Before promising a sibling can be fronted, read `HARNESS-DIVERGENCES.md` §5:
   behelib's hand-rolled JSON-RPC should wait for its beheaxi migration.
4. **Hot reload, kill switch and admin API (sub-project 3) are done on `main`**;
   ship them with the next release and run the e2e stack first; stateless surfaces
   (sub-project 4) are done too.
5. **Open questions carried from the umbrella** — the `fastmcp<4` cap vs
   behesid's FastMCP 4 (blocks only `inproc`), an optional `health` field in the
   beheaxi manifest, per-argument descriptions for `cli` search quality:
   `docs/handoffs/from-BEHEMOTION/PLANNED-BUT-UNBUILT-CAPABILITIES.STATUS.md`
   (untracked, like the rest of `docs/handoffs/`).

Before interface work, read `HARNESS-DIVERGENCES.md` — it is the live list of what is open here.

## History

From 2026-07-29 until **2026-10-07** beherouter ran as a homelab reference
deployment — rootless podman on a service VM behind Caddy, deployed by ansible
from a separate homelab repo with a vendored copy of `src/`, fronting `office`,
`plane` and later `sonarqube`, with LibreChat, Hermes and the Mac clients wired
to it. That deployment, its hostname and its homelab-repo paths are **retired**;
beherouter is now a standalone product. Dated specs, plans, handoffs and the
resolved sections of `HARNESS-DIVERGENCES.md` still describe it — they are
history, not instructions. The failure modes it taught that apply to any podman
host are kept in `docs/DEPLOYMENT.md` § Appendix.

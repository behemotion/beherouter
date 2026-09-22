# Per-user identity — verified callers, per-plugin identity forwarding — design

**Date:** 2026-09-21
**Status:** **implemented and released in `v0.2.1`** (chart `0.1.2`), across
the ten tasks of `docs/superpowers/plans/2026-09-22-per-user-identity.md` plus
the per-surface role gate. 577 tests pass. What this design deliberately left
open is listed at the bottom and tracked in `HARNESS-DIVERGENCES.md`.
**Prompted by:** an external feature request from a bank's AI platform team,
running `v0.2.0` / chart `0.1.1` on Kubernetes with LibreChat in front of Plane
CE and ~2100 directory accounts behind it. Their document is accurate against
this codebase (claims re-verified 2026-09-21, file refs below), and their ask is
a subset of this design.

## The one-sentence problem

**One backend credential means one identity**, and three plugins say so in their
own docstrings (`plugins/plane.py:34-36`, `plugins/gcal.py:11-14`,
`plugins/m365.py`, generalised in `docs/PLUGINS.md:256-258`) — so a gateway
serving a multi-user client attributes every user's writes to one account, and
authorises every user as that account.

This is not a defect. It is the documented consequence of a deliberate choice
(`charts/beherouter/README.md:97-100`: the per-client token boundary is not the
gateway's). What is missing is the **option** to choose otherwise.

## What already exists, precisely

| Fact | Where |
|---|---|
| `SharedTokenVerifier` is the only verifier, hardcoded at boot | `gateway.py:134` |
| It refuses to start without `$BEHEROUTER_GATEWAY_TOKEN` | `auth.py:41-52` |
| A wrong token and a missing one are both a bare 401 | `auth.py:54-56` — correct, unchanged here |
| `build_transport` accepts `headers` for per-session credentials | `backends/mcp.py:23-31` |
| …and **nothing populates it**: no plugin passes headers | `plugins/plane.py:124`, `plugins/office_mcp.py:44` |
| …and it is **silently discarded for `stdio`** | `backends/mcp.py:33-46` never reads `headers` |
| The credential is baked into the transport at attach; calls reconnect against that same transport | `backends/mcp.py:202-216` |
| Every dispatch funnels through one two-method protocol | `models.py:29-30`, applied at `surface.py:146` and `surface.py:329` |
| Per-user pass-through with **more than one header per backend** is a founding requirement — the axis LiteLLM-MCP was rejected on | `docs/DESIGN.md:35-37, 74-78` |
| FastMCP 3.4.5 ships a JWKS verifier (`jwks_uri`/`issuer`/`audience`) | `fastmcp/server/auth/providers/jwt.py:195` |
| FastMCP's `AccessToken` carries **all JWT claims** | `fastmcp/server/auth/auth.py:54-57` |
| `get_http_headers(include={...})` can return allow-listed headers, `authorization` included, and never raises without a request | `fastmcp/server/dependencies.py:410-463` |

So the seam the gateway needs was anticipated in 2026-06 and never wired, because
until now no attached backend could use it. We are not adding a direction; we are
finishing one.

## Decisions settled 2026-09-21

| # | Decision | Consequence |
|---|---|---|
| 1 | **Four identity modes**, selected **per attached surface**: `bearer`, `claims`, `client`, `lookup` | One mechanism, four bindings; a gateway can run a different mode on every surface at once |
| 2 | Gateway accepts **shared token *and* JWT simultaneously** (`CompositeVerifier`) | The four static-token consumers (claude-code, pi, opencode, hermes) keep working on a gateway that also serves JWT-bearing LibreChat users |
| 3 | A surface with a mode **refuses a shared-token caller** | Fail-closed; there is no configuration in which a per-user surface quietly serves a shared identity |
| 4 | Identity is an **explicit parameter** on the executor protocol, not an ambient contextvar | The data flow is visible in every signature and constructible in a test without an HTTP request |
| 5 | `http`, `native` and `cli` backings honour identity; **`stdio` refuses it** in three places | A stdio child's environment is fixed at spawn and `keep_alive=True` reuses it across users; the current silent discard becomes an error |
| 6 | Mode `lookup` resolves credentials from a **mounted secret map, hot-reloaded**, never a DB or a network call | "The gateway is config-file only" stays true (`registry.py:1`) |
| 7 | A plugin must **declare** which modes it supports, inertly | `registry-lint` refuses an unsupported combination offline, before a deploy |
| 8 | There is **no `require = false`** knob | The client's `forward.auth = true/false` shape is deliberately not adopted: a boolean invites the "attached, believed per-user, actually shared" state |
| 9 | Attach, catalogue and `health --deep` keep using the **deployment credential** | A per-user catalogue is not built: the catalogue is a deployment property, the calls are per-user |
| 10 | No JWKS code of our own | FastMCP's `JWTVerifier` is configured, not reimplemented |

Out of this cut by explicit decision: an `http` backing variant for the `plane`
plugin, RFC 8693 token exchange, a per-user catalogue, and per-user OAuth
bootstrap flows for the calendar surfaces (the mechanism lands; populating 2100
consents does not).

## Architecture

Four pieces. Only the third touches existing call paths.

### 1. Identity in — `auth.py`

`SharedTokenVerifier` is unchanged, including its bare-401 behaviour. Two
additions:

```python
SHARED_CLIENT_ID = "beherouter-shared"   # already the value at auth.py:59

class CompositeVerifier(TokenVerifier):
    """Try each verifier in order; the first AccessToken wins."""
```

`build_verifier()` reads the gateway's auth configuration from **the
environment**, consistent with every other gateway secret, and **not** from a
`[auth]` table in `registry.toml`: `load_registry` treats every top-level table
as a surface (`registry.py:91-98`), so `[auth]` would be parsed as a surface
named `auth` and rejected as having unknown keys.

| Variable | Default | Meaning |
|---|---|---|
| `BEHEROUTER_AUTH_MODE` | `shared` | `shared` \| `oidc` \| `both` |
| `BEHEROUTER_OIDC_ISSUER` | — | required when the mode includes `oidc`; checked as `iss` |
| `BEHEROUTER_OIDC_AUDIENCE` | — | required when the mode includes `oidc`; checked as `aud` |
| `BEHEROUTER_OIDC_JWKS_URI` | — | required when the mode includes `oidc` |
| `BEHEROUTER_OIDC_REQUIRED_SCOPES` | — | optional, comma-separated |

⚠️ **The JWKS URI is explicit, never discovered.** Deriving it from the issuer's
`.well-known/openid-configuration` would mean a network call before the routes
exist, and this gateway's rule is that a boot-time failure kills `/healthz` for
every surface. `JWTVerifier` fetches and caches the JWKS lazily, on the first
token it verifies, which is the correct side of the attach boundary. A
convention-derived path (`/protocol/openid-connect/certs`) is also rejected: it
is Keycloak-shaped, and nothing else here assumes an IdP vendor.

Validation is a string check at startup: mode `oidc`/`both` without issuer,
audience or JWKS URI is a `UsageError` at boot — the same discipline as a missing
gateway token.

A caller authenticated by the shared token is identified by
`AccessToken.client_id == SHARED_CLIENT_ID`; anything else carries claims and is
a user. That discriminator is asserted by a test so the sentinel cannot drift
into being decorative.

### 2. The declaration — `plugins/spec.py` and `registry.toml`

A plugin declares its identity capability the same inert way it declares pins and
probes:

```python
@dataclass(frozen=True)
class IdentitySupport:
    modes: tuple[str, ...] = ()      # subset of ("bearer", "claims", "client", "lookup")
    target: str = ""                 # "header" | "env" | "credential"
    accepts: tuple[str, ...] = ()    # allowed target keys
    doc: str = ""
```

`accepts` closes the target namespace. Empty means "any name" for the `header`
and `env` targets, where the backend's own vocabulary is open. For the
`credential` target it is **required and validated against `PluginSpec.env`**:
a native plugin's credentials are a closed set it already declares, so a typo'd
name is a lint error rather than a credential silently never applied.

`PluginSpec.identity: IdentitySupport = IdentitySupport()` — **the default is no
modes**, so a plugin nobody has audited for per-user use cannot be configured for
it. This is the client's "a plugin declares that it supports forwarding" ask,
generalised from a boolean to a mode set and a target namespace.

`target` is the namespace the mapping's left-hand side lives in, and it follows
the backing:

| Backing | `target` | The mapping's keys are | Applied by |
|---|---|---|---|
| `http` | `header` | HTTP header names | per-call transport headers |
| `cli` | `env` | environment variable names | subprocess environment |
| `native` | `credential` | logical credential names — **must be `PluginSpec.env` names** | the plugin's own provider factory |
| `stdio` | — | refused | — |

`RegistryEntry` gains one field, `identity: dict | None`, which
`load_registry`'s unknown-key check picks up for free (it is derived from the
dataclass fields, `registry.py:89`). The four modes as an operator writes them:

```toml
# 1. bearer — forward the verified caller's own token
[example-a.identity]
mode = "bearer"
header = "authorization"      # default
prefix = "Bearer "            # default; "" sends the raw token

# 2. claims — assert identity from named JWT claims
[example-b.identity]
mode = "claims"
  [example-b.identity.map]
  "x-remote-user" = "email"   # target key <- claim name
  "x-remote-id"   = "sub"

# 3. client — forward a per-user credential the client supplies
[example-c.identity]
mode = "client"
  [example-c.identity.map]
  "x-api-key"        = "x-user-api-key"      # target key <- CLIENT header name
  "x-workspace-slug" = "x-user-workspace"

# 4. lookup — resolve a per-user credential from the mounted map
[example-d.identity]
mode = "lookup"
key = "email"                               # the claim identifying the user
path = "/etc/beherouter/identity-map.toml"  # or $BEHEROUTER_IDENTITY_MAP
  [example-d.identity.map]
  "x-api-key" = "api_key"                   # target key <- logical credential in the map
```

⚠️ **Mode `client` is an allow-list, in both directions.** Only the client header
names named on the right-hand side are read (`get_http_headers(include=...)`),
and only the target keys named on the left are written. A gateway that forwarded
whatever headers a client sent would be a header-smuggling hole in front of every
backend.

### 3. The seam — `models.py`, `surface.py`, the four executors

`Executor` gains one keyword-only optional parameter:

```python
class Executor(Protocol):
    async def run(self, verb: str, args: dict, *, identity: "CallIdentity | None" = None) -> dict: ...
```

`None` means "no identity mode on this surface", and every executor's behaviour
under `None` is byte-identical to today. That is the whole compatibility story.

What flows is already-materialised, so no executor parses configuration:

```python
@dataclass(frozen=True)
class CallIdentity:
    subject: str                     # the verified caller, for logs and cache keys
    headers: Mapping[str, str]       # http backings apply these
    env: Mapping[str, str]           # cli backings apply these
    credentials: Mapping[str, str]   # native backings apply these
    cache_key: str                   # sha256 over subject + material; carries no secret
```

There is deliberately no `kind` field: a shared-token caller never produces a
`CallIdentity` at all (it is refused upstream, decision 3), so carrying a
`"shared"` variant here would be a state no code path can reach and every
`isinstance`-style check would have to pretend to handle.

A new module `identity.py` owns resolution and materialisation:

- `IdentityPolicy.from_entry(entry, spec)` — inert, no I/O, built in
  `gateway.build_surfaces` (which holds the `RegistryEntry`; `load_backend`
  returns only a `Backend`) and passed to `build_surface` beside `auth`.
  ⚠️ `health.check_entry` builds a surface too (`health.py:68-78`) and passes
  **no** policy — a health sweep has no HTTP request to resolve an identity from,
  and must keep probing with the deployment credential.
- `policy.resolve()` — called **once per call, in `surface.py` only**. This is the
  single place FastMCP's request context is read (`get_access_token()`,
  `get_http_headers(include=...)`); executors never touch it, which is what keeps
  them unit-testable and what keeps `health --deep` and the CLI — neither of which
  has a request — working unchanged.
- `policy.resolve()` returns `None` when the surface has no mode, so the dispatch
  site is one line in both `_make_pinned_tool` and `run_tool`.

Per-backing application:

- **http** (`ReconnectingMCPExecutor`): builds its transport per call when
  `identity.headers` is non-empty, layering identity headers **over**
  `backing.env` (which `backends/mcp.py:51` already merges as headers). No
  transport cache: the executor already opens a session per call, so a cache
  would add eviction concerns to save a constructor.
- **native** (calendar): `build_backend` gains an optional
  `provider_factory(credentials) -> provider`, and `CalendarExecutor` keeps a
  **bounded LRU of providers keyed by `cache_key`** (default 128). A provider
  holds a refreshed OAuth access token, which is exactly the thing worth keeping
  between a user's calls; an LRU bound is what stops 2100 users from becoming
  2100 live token sets.
- **cli** (`CLIExecutor`): passes `env=os.environ | identity.env` to
  `create_subprocess_exec` (it inherits the parent environment today by omitting
  `env=`).
- **stdio**: `build_transport` **raises** `UsageError` when handed headers, closing
  the silent discard at `backends/mcp.py:33-46`. Defence in depth — `validate_entry`
  and `registry-lint` refuse the combination before this can be reached.

### 4. The secret map — mode `lookup`

TOML, because `tomllib` is already the only config parser here and `registry.toml`
set the precedent:

```toml
["alice@example.com"]        # the value of the configured `key` claim
api_key = "plane_pat_…"      # logical credential names, as the entry's map cites them

["bob@example.com"]
api_key = "plane_pat_…"
```

Lifecycle:

- **Read on the call path, never at attach.** A typo'd path or a malformed file
  must not crash-loop the gateway and take `/healthz` down; it degrades to that
  one surface's calls failing.
- **Hot reload by stat.** One `os.stat` per resolve; the parsed map is cached
  against `(st_mtime_ns, st_size)`. A rotated K8s Secret or a re-rendered vault
  file is picked up without a restart, which is the difference between rotating a
  user's PAT and redeploying the gateway.
- **Pre-deploy checkable.** `registry-lint` reads the path when it exists locally
  (it is already a filesystem-touching, network-free gate), and `health --deep`
  reports the map's state per surface, so an operator learns about a missing map
  from a check rather than from a user's failed call.
- **Never logged, never echoed.** The map's values are credentials; `cache_key` is
  a digest so that not even an in-memory cache key holds one.

## Data flow, one identity-bearing call

```
client → Caddy/Ingress (per-client token boundary, unchanged)
       → CompositeVerifier: shared token? JWT via JWKS? → AccessToken(+claims)
       → surface.py dispatch
           policy.resolve():
             kind == "shared" and mode != none      → AuthError (fail closed)
             bearer  : raw token            → headers
             claims  : AccessToken.claims   → headers / env / credentials
             client  : allow-listed headers → headers / env / credentials
             lookup  : claims[key] → map    → headers / env / credentials
             any named source missing       → AuthError (never partial)
       → executor.run(verb, args, identity=CallIdentity(...))
           http   : transport headers, over backing.env
           cli    : subprocess env
           native : provider from the per-identity LRU
           stdio  : UsageError
       → backend
```

## Failure modes, and where each is caught

| Situation | Caught by | Result |
|---|---|---|
| Mode named against a plugin that declares no modes | `registry-lint`, `validate_entry` | `UsageError` offline, before deploy |
| Mode named against an `stdio` plugin | `registry-lint`, `validate_entry`, `build_transport` | `UsageError` in three places |
| Mode named while `BEHEROUTER_AUTH_MODE=shared` | boot; `registry-lint` when the variable is visible to it | Refused: a gateway that cannot verify a user cannot require one. ⚠️ Lint runs on a workstation and in an init container, where the gateway's env may be absent — so boot is the authority and lint is the early warning, the reverse of every other row |
| Unknown mode, or a target key outside `IdentitySupport.accepts` | `registry-lint`, `validate_entry` | `UsageError` naming the allowed set |
| Shared-token caller reaches a mode-bearing surface | `policy.resolve()` | `AuthError`; no backend call is made |
| A named claim is absent from the token | `policy.resolve()` | `AuthError` naming the claim; **no partial material is ever sent** |
| A named client header is absent | `policy.resolve()` | `AuthError` naming the header |
| The user is absent from the secret map | `policy.resolve()` | `AuthError` naming the surface and the key claim |
| The secret map is missing or malformed | `policy.resolve()` | `Unavailable` for that surface only; other surfaces keep serving |
| Backend rejects the forwarded identity | existing funnel | `UsageError` for a tool-level error, `Unavailable` for a dead backend (`backends/mcp.py:92-107`) |

⚠️ **There is no fallback path anywhere in that table.** Every row either
refuses or reports. The one thing this design will not do is serve a call with
the deployment credential on a surface an operator configured as per-user.

## Attach, catalogue, health

Unchanged, deliberately:

- The **deployment credential stays required** exactly as today — `validate_entry`
  still demands every `PluginSpec.env` name (`registry.py:69-81`). Attach uses it
  to list the catalogue; per-call identity material overrides it per key.
- `health --deep`'s probe still calls the backend with the deployment credential.
  ⚠️ **A green probe therefore says nothing about any user's credential** — it
  proves the bootstrap credential, exactly as it does now. This goes in the
  command's own output wording, not only in prose.
- `health --deep` gains a per-surface `identity` field: the configured mode, and
  for `lookup` the map's state (`ok` / `missing` / `unparsable`) and its entry
  count. Never a key, never a value.
- One structured log line per identity-bearing call: surface, verb, `subject`,
  mode, and the **names** of the material keys applied. Never the raw
  token, never the claims wholesale (they carry PII the gateway has no reason to
  persist), never a credential.

## Testing

TDD, against the existing 473-test suite. The invariants worth naming:

**Verifier**
- shared token accepted under `shared` and `both`, rejected under `oidc`
- a JWT signed by the test key accepted under `oidc` and `both`, rejected under `shared`
  (FastMCP ships `RSAKeyPair` in `providers/jwt.py` — no fixture IdP needed)
- wrong `aud`, wrong `iss`, expired, unsigned → rejected, still a bare 401
- `AccessToken.client_id == SHARED_CLIENT_ID` is the shared/user discriminator

**Resolution and fail-closed**
- shared caller on a mode-bearing surface → `AuthError` **and the executor is never called** (spy)
- missing claim / missing client header / unmapped user → `AuthError`, and no partial material
- `client` mode reads only allow-listed headers: an unnamed header the client sent does not reach the backend
- `bearer` prefix handling, including `prefix = ""`

**Per-backing**
- `build_transport(stdio-backing, headers={...})` **raises** — the regression test for the silent discard
- http: identity headers reach the transport and override `backing.env` for the same key
- cli: identity env reaches the subprocess (the existing fixture CLI, echoing its environment)
- native: two identities get two providers; the LRU is bounded; `gcal`/`m365` still expose byte-identical schemas

**Lint**
- every row of the failure-modes table that says `registry-lint`, asserted offline with no network and no attach

**Map**
- hot reload on mtime change; unparsable → `Unavailable`; values never appear in a log record or a `cache_key`

**Compatibility**
- the whole existing suite passes unmodified: no `[surface.identity]` and
  `BEHEROUTER_AUTH_MODE` unset ⇒ `identity=None` at every dispatch

## Files touched

| File | Change |
|---|---|
| `src/beherouter/auth.py` | `CompositeVerifier`, `build_verifier()`, env config, `SHARED_CLIENT_ID` |
| `src/beherouter/identity.py` | **new** — `CallIdentity`, `IdentityPolicy`, materialisation, the secret map |
| `src/beherouter/plugins/spec.py` | `IdentitySupport`, `PluginSpec.identity` |
| `src/beherouter/registry.py` | `RegistryEntry.identity`, offline validation |
| `src/beherouter/models.py` | `Executor.run(..., *, identity=None)` |
| `src/beherouter/surface.py` | resolve once per call; enforce; pass down at both dispatch sites |
| `src/beherouter/gateway.py` | build the verifier by mode; build and pass the policy |
| `src/beherouter/backends/mcp.py` | per-call headers for `http`; **raise** for `stdio` + headers |
| `src/beherouter/backends/cli.py` | identity env on the subprocess |
| `src/beherouter/plugins/calendar/{__init__,executor}.py` | provider factory + bounded per-identity LRU |
| `src/beherouter/plugins/{gcal,m365}.py` | declare `IdentitySupport` (target `credential`) |
| `src/beherouter/health.py` | per-surface `identity` field; probe wording |
| `src/beherouter/cli/app.py` | `registry-lint` rules |
| `charts/beherouter/*` | auth-mode values, optional secret-map mount, README on the new boundary |
| `docs/PLUGINS.md`, `docs/DEPLOYMENT.md`, `AGENTS.md`, `HARNESS-DIVERGENCES.md` | the mechanism, the operator's three modes, the retired caveat where it is retired |

## Follow-ups this design deliberately leaves open

1. ~~**`plane` needs an `http` backing to be per-user at all.**~~ Done
   2026-09-22: two plugins, `plane-http` (mode `bearer`) and `plane-http-apikey`
   (mode `client`), sharing `plane.py`'s pin list and probe. ⚠️ The assumption
   in this line was wrong in one respect, and measurement is what caught it:
   plane-mcp-server 0.3.2's `/http` mount is an OAuth **proxy** that accepts only
   tokens it minted itself, so `bearer` cannot bind it until Plane's own JWT
   rework ships behind a backend that honours a forwarded token. The mount that
   works today is `/http/api-key`, with a per-caller PAT. Evidence: `tests/e2e/`.
2. **RFC 8693 token exchange** as a fifth mode, for backends that need an
   audience-scoped token rather than ours.
3. **Per-user OAuth for the calendar surfaces** — the mechanism lands here
   (target `credential`), the consent flows do not.
4. ~~**`BEHEROUTER_PLANE_TOKEN` collides with itself across two meanings.**~~
   Closed: every example in the repo now names the backend PAT
   `BEHEROUTER_PLANE_API_KEY` (what `pluginconfig.token_var` emits), and
   `registry-lint` *warns* — never refuses — when an entry points a backend
   credential at `BEHEROUTER_<SURFACE>_TOKEN`, the name `clientconfig.token_var`
   reserves for the CLIENT's gateway bearer. The live homelab registry still uses
   the colliding name; rotating it is a homelab-repo change (`AGENTS.md` § Plugins).
5. ~~**Publish the chart as an OCI artifact.**~~ Closed: `release.yml` pushes it to
   `ghcr.io/behemotion/charts/beherouter` after the image, refusing a chart version
   that is already published.
6. **The published `tools` array is not gated.** The read-only meta-tools now
   refuse a caller a surface would not serve, but the frozen array a host reads at
   connect time is captured at attach and is served to anyone the gateway
   authenticates. Gating it means a FastMCP `on_list_tools` middleware and a
   decision about what a host should see when it may call nothing.

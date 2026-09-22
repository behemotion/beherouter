# Per-user identity

> Verify the caller, and forward *their* identity to a backend — instead of one
> deployment credential doing every user's work.
>
> **Default behaviour is unchanged.** With no `BEHEROUTER_AUTH_MODE` and no
> `[surface.identity]`, a gateway behaves exactly as it did before this existed:
> one shared token, one backend credential, `identity=None` at every dispatch.

Design record: `docs/superpowers/specs/2026-09-21-per-user-identity-design.md`.
Plugin authoring: [`PLUGINS.md`](PLUGINS.md). Deployment: [`DEPLOYMENT.md`](DEPLOYMENT.md).

## 1. What this gives you, and what it does not

**It gives you** a verified caller (a JWKS-checked OIDC JWT, accepted *beside*
the shared token), and a per-user credential at the backend, chosen per attached
surface. Four surfaces can run four different modes on one gateway.

**It does not give you:**

- **A per-user catalogue.** Attach, the searchable catalogue and `health --deep`
  all keep using the deployment credential. The catalogue is a property of the
  deployment; only the *calls* are per-user. A user who cannot see a tool in
  their own Plane will still see it listed.
- **A per-user probe.** ⚠️ **A green `health --deep` probe proves the deployment
  credential and nothing else.** The record says so in the output itself —
  `identity.probe_scope: "deployment-credential"` — so an operator reading the
  JSON does not have to have read this page. Only a real per-user call proves a
  per-user credential.
- **Authorization.** A role gate (§6) exists, and it is ergonomics. The control
  is the backend's own verification.

## 2. The two halves

Identity **in** is gateway-wide; identity **out** is per surface. They are
configured in different places and fail in different ways.

| Variable | Default | Meaning |
|---|---|---|
| `BEHEROUTER_AUTH_MODE` | `shared` | `shared` \| `oidc` \| `both` |
| `BEHEROUTER_OIDC_ISSUER` | — | required when the mode includes `oidc`; checked as `iss` |
| `BEHEROUTER_OIDC_AUDIENCE` | — | required when the mode includes `oidc`; checked as `aud` |
| `BEHEROUTER_OIDC_JWKS_URI` | — | required when the mode includes `oidc` |
| `BEHEROUTER_OIDC_REQUIRED_SCOPES` | — | optional, comma-separated, gateway-wide |
| `BEHEROUTER_OIDC_ROLES_CLAIM` | — | dotted path to the roles claim; required by §6 |
| `BEHEROUTER_IDENTITY_MAP` | — | default path for mode `lookup` (§5) |

`both` is the interesting one: the shared token and a user JWT are accepted **at
the same time**, so the consumers that cannot mint a JWT keep working beside
those that can.

⚠️ **The JWKS URI is explicit and never discovered from the issuer.** Resolving
`.well-known/openid-configuration` at boot would put a network call before the
routes exist, and a slow IdP would take `/healthz` down with it. FastMCP fetches
and caches the key set lazily, on the first token it sees.

Identity **out** is one table per surface in `registry.toml`:

```toml
[office]
plugin = "office-mcp"
  [office.identity]
  mode = "bearer"
```

## 3. The four modes

A surface has **one** mode, or none. There is deliberately **no `require =
false`**: a surface either requires a verified user or does not mention
identity. A boolean would invite the state this whole mechanism exists to
prevent — attached, believed per-user, actually shared.

### `bearer` — forward the caller's own token

```toml
  [plane.identity]
  mode = "bearer"
  # optional; these are the defaults
  header = "authorization"
  prefix = "Bearer "
```

The right mode when the backend verifies the **same** tokens your gateway does —
a backend with an authentication class checking your realm's JWKS. Nothing is
stored and no credential is mapped; the caller's token is passed through.

### `claims` — assert who the caller is

```toml
  [wiki.identity]
  mode = "claims"
    [wiki.identity.map]
    "x-remote-user" = "email"
    "x-remote-id" = "sub"
```

Target key → claim name. The right mode for a backend that trusts the gateway to
say who is calling (a `REMOTE_USER`-style contract), not one that authenticates
the user itself. A missing claim refuses the call — a **partial** header set is
worse than a refusal, because it asserts an identity the caller does not have.

### `client` — take the credential from the client

```toml
  [plane.identity]
  mode = "client"
    [plane.identity.map]
    "x-api-key" = "x-user-api-key"
    "x-workspace-slug" = "x-user-workspace"
```

Target key → the **client** header to read it from. The right mode when each
user holds their own PAT and the client can send it (LibreChat's
`customUserVars`, for example).

⚠️ The map's values are an **allow-list**. Only the headers named here are ever
read from the request; everything else the client sent is dropped. A gateway
forwarding arbitrary client headers would be a header-smuggling hole in front of
every backend.

### `lookup` — resolve the caller's credential from a map

```toml
  [gcal.identity]
  mode = "lookup"
  key = "email"            # the claim that identifies the caller
  path = "/etc/beherouter/identity-map.toml"
    [gcal.identity.map]
    refresh_token = "refresh_token"
```

The right mode when the backend credential is a real secret the user cannot
hand you per request — an OAuth refresh token, a minted PAT. See §5.

### Where a mode's output lands

| Backing | `target` | The map's keys are | Applied as |
|---|---|---|---|
| `http` | `header` | HTTP header names | per-call transport headers, over the attach-time ones |
| `cli` | `env` | environment variable names | subprocess environment, merged over the gateway's own |
| `native` | `credential` | the plugin's own declared credential names | a per-identity provider behind a bounded cache |
| `stdio` | — | — | **refused** (§7) |

A plugin declares which modes it supports and which target names it accepts; an
entry naming anything else is refused offline by `registry-lint`. A plugin that
declares nothing cannot be configured for per-user use at all.

## 4. What a green attach does not mean

Attach performs **no** identity resolution and **no** map read. Both happen on
the call path only. That is deliberate: an attach failure crash-loops the whole
gateway and takes every other surface and `/healthz` with it, so a typo'd map
path degrades one surface's calls instead.

`registry-lint` and `health --deep` are where an operator finds out early.

## 5. The identity map

A TOML file, keyed by the value of the surface's `key` claim:

```toml
["alice@example.com"]
refresh_token = "…"

["bob@example.com"]
refresh_token = "…"
```

The inner names are **logical** credential names, exactly as the entry's
`[surface.identity.map]` cites them — never the backend's own variable names.

- **Hot-reloaded by stat.** One `os.stat` per resolve; the parsed content is
  cached against `(mtime_ns, size)`. Rotating a user's credential needs no
  restart.
- **A missing or malformed map degrades one surface.** It is `Unavailable` for
  that surface's calls, not a dead gateway. `health --deep` reports
  `identity.map.state` as `ok`, `missing` or `unparsable`.
- **A caller absent from the map is refused.** The error names the *claim* that
  was matched on, never its value.

⚠️ **On Kubernetes, a `subPath` mount does not hot-reload.** kubelet updates the
projected directory, not a file mounted through `subPath`, so rotating a
credential in the Secret needs a pod restart. Mount the directory instead if you
need live rotation — the stat-based reload is what makes that variant work, and
it is what makes a vault-rendered file on a VM reload with no restart at all.

## 6. Role gating — `[surface.authz]`

```toml
# gateway-wide, in the environment, because every IdP puts roles elsewhere
#   BEHEROUTER_OIDC_ROLES_CLAIM=realm_access.roles

[plane]
plugin = "plane"
  [plane.authz]
  require_roles = ["ai-plane-access"]
```

⚠️ **This carries no security weight.** The backend's own verification is the
control. The gate turns an opaque backend `401` into a sentence naming the
surface, at the edge, before the call is made.

- **Roles are not scopes.** Scopes arrive in `scope`; roles arrive in a
  provider-specific claim — `realm_access.roles` on Keycloak, `roles` on Entra,
  `groups` elsewhere — so the path is configuration. There is **no default**:
  guessing would special-case one vendor, and an unset path makes a gate fail
  closed.
- A **list** and a **space-delimited string** are both accepted at the leaf.
- **Every** listed role must be held, not any one of them.
- It is **separate from `identity`** on purpose: a surface may gate while
  forwarding nothing. That is also why a gate works on a `stdio` surface, which
  can never carry an identity.
- A shared-token caller is refused by a gate, exactly as by an identity mode.

## 7. The rules that will refuse you

Offline, from `registry-lint` (and from `validate_entry`, so also at boot):

| Configuration | Refused because |
|---|---|
| A mode on a plugin declaring no `IdentitySupport` | The plugin has not been audited for per-user use |
| A mode the plugin does not declare | e.g. `bearer` on a plugin that only supports `lookup` |
| Any mode on a **`stdio`** backing | See below |
| A map key outside the plugin's `accepts` | A `native` plugin's credentials are a closed set |
| `claims`/`client`/`lookup` with an empty map | The mode has nothing to forward |
| `bearer` with a map | It takes `header` and `prefix` |
| `lookup` with no `path` and no `$BEHEROUTER_IDENTITY_MAP` | Nowhere to read from |
| A `lookup` map that does not exist, where lint can see it | It would fail every call |
| `require_roles` that is not a non-empty array of names | Malformed gate |

At boot, where the gateway's own environment is authoritative:

| Configuration | Refused because |
|---|---|
| A per-user surface with `BEHEROUTER_AUTH_MODE=shared` | A gateway that cannot verify a user cannot require one |
| `require_roles` with `BEHEROUTER_AUTH_MODE=shared` | Same |
| `require_roles` with no `BEHEROUTER_OIDC_ROLES_CLAIM` | The gate could only fail every call |

`registry-lint` repeats the boot checks **only where the variables are visible**:
lint runs on a workstation and in an init container, where the gateway's
environment may be absent, and a default-driven refusal there would fail a
perfectly good registry. Boot is the authority; lint is the early warning.

⚠️ **`stdio` can never be per-user.** A subprocess environment is fixed at spawn
and `keep_alive=True` reuses that subprocess across callers, so per-request
material cannot reach it. This used to be *silently discarded* —
`build_transport` accepted a `headers` argument its stdio branch never read, so
a per-user configuration attached green and forwarded nothing. It now raises, in
three places: at lint, at `validate_entry`, and in `build_transport` itself.
Attach such a backend over `http` instead.

## 8. Operating it

```bash
beherouter registry-lint --path registry.toml   # every rule in §7, offline
beherouter health --deep --json                 # per-surface `identity` block
```

A `health --deep` record carries:

```json
{"name": "gcal",
 "identity": {"mode": "lookup",
              "probe_scope": "deployment-credential",
              "require_roles": ["ai-calendar-access"],
              "map": {"state": "ok", "entries": 12}}}
```

An applied identity is logged with the subject, the mode and the **names** of
the material applied — never a value.

On Kubernetes, `auth.*` and `identityMap.*` in the chart's values render the
variables above into **both** the Deployment and the `registry-lint` hook Job,
so the pre-deploy gate sees the environment that will actually serve. A pure
`auth.mode=oidc` deployment no longer carries a shared gateway token it would
never check.

⚠️ **Every write is still attributed to one identity unless a surface has a
mode.** Making a surface available to every user did not make it per-user; a
`[surface.identity]` table is what does that.

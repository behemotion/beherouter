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
surface. Five surfaces can run five different modes on one gateway.

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
- **A private tool list on an ungated surface.** On a surface with a role or
  audience gate (§6), a caller the gate refuses gets an empty `tools/list`. A
  surface with only an identity mode, or with `hide_tools = false`, serves its
  frozen published `tools` array to anyone the gateway authenticates. And the
  list is hidden, not secret: a host that already cached it keeps it.
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

## 3. The five modes

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

### `exchange` — trade the caller's token for one addressed to the backend

```toml
  [crm.identity]
  mode = "exchange"
  token_url = "https://idp.example.com/realms/acme/protocol/openid-connect/token"
  audience = "crm-api"                  # and/or resource = "https://crm.internal/"
  scope = "crm.read"                    # optional; string or array
  subject_token_type = "jwt"            # default; or "access_token"
  client_auth = "client_secret_basic"   # default; or "client_secret_post"
  client_id = "beherouter"              # literal or ${VAR}
  client_secret = "${BEHEROUTER_CRM_EXCHANGE_SECRET}"   # MUST be a ${VAR}
  # header = "authorization", prefix = "Bearer " — as for `bearer`
```

OAuth 2.0 Token Exchange (RFC 8693). The right mode when the backend verifies
the same IdP but, correctly, refuses a token addressed to the **gateway**: on
each call the gateway, authenticated as its own OAuth client, posts the
caller's verified JWT to `token_url` and forwards the issued `access_token`.
The backend sees a token minted for it, carrying the caller's identity; the
IdP's exchange policy decides who may get one. Design record:
`docs/superpowers/specs/2026-10-07-token-exchange-design.md`.

- **Header backings only** (`http`, and `inproc` such as `openapi`). `cli` and
  `native` are refused: no CLI plugin takes a bearer, and a calendar provider
  needs a refresh token, which an exchange does not yield.
- **Cached** per (surface, digest of the caller's token) until `expires_in`
  minus 30 s, in a bounded LRU; a response without `expires_in` is not cached.
  Concurrent calls for one caller share one exchange. Nothing is keyed or
  logged by a raw token.
- **No fallback.** `invalid_grant` / `invalid_target` refuse the call as an
  auth error; any other 4xx (`invalid_client`, `invalid_scope`, …) is a
  configuration error; a 5xx, 429, timeout or network error is `Unavailable`.
  Each names the surface and the OAuth `error` code only — never a token and
  never the IdP's `error_description`. A failed exchange never sends the call.
- **The secret is resolved per exchange**, so an unset variable degrades this
  surface's calls rather than refusing boot; `health --deep` reports
  `identity.exchange.client_secret: "set" | "unset"`.
- The exchange runs on the call path, after the backing's own guard, so a
  call refused at the gateway costs no round trip to the IdP.

### Where a mode's output lands

| Backing | `target` | The map's keys are | Applied as |
|---|---|---|---|
| `http` | `header` | HTTP header names | per-call transport headers, over the attach-time ones |
| `inproc` | `header` | HTTP header names | per-call headers on the in-process server's outbound client |
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
- It covers **`search_tools`, `describe_tool`, `run_tool` and `context_cost`**
  as well as every published tool: a surface that refuses your calls also
  refuses to enumerate itself to you, and refuses *before* re-listing the
  backend, so a caller you excluded cannot drive traffic to it. The gate runs
  without materialising anything, so a `lookup` surface whose map is unreadable
  still answers a search for a caller who holds the role. The published
  `tools` array is hidden from a refused caller too — see *Hiding the tool
  list* below.
- It is **separate from `identity`** on purpose: a surface may gate while
  forwarding nothing. That is also why a gate works on a `stdio` surface, which
  can never carry an identity.
- A shared-token caller is refused by a gate, exactly as by an identity mode.

### Per-surface audience

```toml
# gateway-wide: ANY of these (comma-separated), checked by the JWT verifier
#   BEHEROUTER_OIDC_AUDIENCE=plane-mcp,wiki-mcp

[plane]
plugin = "plane-http"
  [plane.authz]
  audience = "plane-mcp"          # or ["plane-mcp", "plane"]: any one of them
```

`BEHEROUTER_OIDC_AUDIENCE` is gateway-wide. With several teams' surfaces on one
gateway, every surface used to accept every team's tokens, under an audience
name that fit only one of them. `audience` narrows a surface to tokens whose
`aud` names it. It is checked **in addition to** the gateway-wide verification,
never instead of it, and the gateway-wide value may now be a list. The gate
behaves like `require_roles`: it applies to the meta-tools too, it refuses a
shared-token caller, and it is refused at boot (and at lint, where visible) on
an `auth.mode: shared` gateway. It needs no `BEHEROUTER_OIDC_ROLES_CLAIM`,
because `aud` is a standard claim. A refusal names the **expected** audience,
never the token's.

### Hiding the tool list

```toml
[plane]
plugin = "plane"
  [plane.authz]
  require_roles = ["ai-plane-access"]
  hide_tools = true                 # the default; false restores the old listing
```

An MCP host puts every listed tool in front of the model, for every user. A
gated surface that lists its tools to a caller it refuses teaches their model
to offer tools that can only fail, and the user then reads a refusal naming a
role they cannot request. So on a surface with `require_roles` or `audience`,
a caller who fails the gate sees an empty server:

| MCP method | Caller refused by the gate | Caller who passes |
|---|---|---|
| `initialize` | `200`, unchanged | `200` |
| `tools/list` | `{"tools": []}` | the published array, unchanged |
| `tools/call` (pinned or meta-tool) | the refusal naming the missing role, unchanged | the call |

- **The call is still the gate.** Hiding is cosmetic; a client that calls a
  tool it was never shown gets the same refusal as before.
- **`tools/list` never errors.** An error there makes most hosts mark the whole
  server failed. A gate that cannot be evaluated at all (no roles claim
  configured, which boot refuses anyway) lists nothing and logs a warning.
- **It gates, it never materialises**, like the meta-tools: a `lookup`
  surface with an unreadable map still lists for a caller who holds the role.
- **A shared-token caller** under `BEHEROUTER_AUTH_MODE=both` fails the gate,
  so it sees an empty list too.
- **A mode alone hides nothing.** A mode says *whose credential* a call
  carries, not *who may* call; only a role or audience gate hides.
- ⚠️ **Hosts cache the list.** LibreChat re-lists at connect and reconnect, not
  per message. A role granted mid-session appears after a reconnect or a
  re-login; a role revoked mid-session leaves the tools listed until then, and
  their calls refused. Neither is a bug.
- `hide_tools = false` restores the old behaviour, for a host that would
  rather show the tools and let the call fail. `hide_tools` without
  `require_roles`, `audience` or per-tool `tools` gates (§6c) beside it is
  refused: there is nothing to gate the listing on. `health --deep --json` reports it under `identity.hide_tools`.

### 6c. Per-tool gates and confirmation

```toml
[plane]
plugin = "plane"
  [plane.authz]
  require_roles = ["ai-plane-access"]      # the surface gate, as before
  confirm_mutating = true                  # a human says yes to every write
  confirm_exempt = ["comment"]             # ...except these tools
    [plane.authz.tools.workitem]
    require_roles = ["plane-writer"]       # on top of the surface gate
```

**Per-tool role gates.** `[surface.authz.tools.<name>] require_roles` names tools
by their catalogue name, `run_tool`'s inner tool included. **Every** role must be
held, **in addition to** the surface gate, with the same roles claim
(`BEHEROUTER_OIDC_ROLES_CLAIM`, no default). Boot and `registry-lint` refuse a
tool gate when that claim is unset or the gateway is `shared`-only, like the
surface gate.

- **A tool gate does not make the surface require a verified user.** A
  shared-token caller keeps every ungated tool and is refused only the gated
  ones, with `unauthenticated`.
- **Hiding.** Under `hide_tools` (default `true`; now valid beside `tools` alone)
  a caller who fails a tool gate does not see that tool in `tools/list` or
  `search_tools`. `describe_tool` and `run_tool` on it answer `missing_role` (or
  `unauthenticated` for a shared-token or anonymous caller), not
  `unknown_tool`: hiding is cosmetic, the call is the gate.
- Like the surface gate it is ergonomics; the backend's verification is the control.

**Confirming writes.** `confirm_mutating = true` makes the gateway ask the
caller's human, through MCP elicitation, before a mutating call reaches the
backend. A tool needs it when its `mutating` is true **or unknown** (the backend
sent no `readOnlyHint`: unknown fails closed), unless named in `confirm_exempt`
(valid only with `confirm_mutating`). The question names the tool and surface and
shows the arguments **to the caller's own client only**; they are never logged.
Every argument name is always shown; a long value is cut to its first 300
characters (fewer when there are many arguments) and marked `…(+K chars)`, and
the question then says `Arguments (truncated, N of M characters shown)`, so a
human is never asked to approve arguments that were silently hidden. A
refusal is `confirmation_required` (kind `refused`) with `context.confirmation`:

| `confirmation` | Meaning |
|---|---|
| `unsupported` | the client has no elicitation capability, or errored when asked |
| `declined` | the user declined, cancelled or answered no |
| `timeout` | no answer within 300 s |

`describe_tool` adds `"requires_confirmation": true`; the published pinned tool
descriptions are unchanged.

⚠️ **A client without elicitation is refused on every mutating call.** An agent
re-sending a flag is not a human confirming, so there is no fallback. Consumer
support for elicitation is **unknown (not verified)** for all five (LibreChat,
Hermes, pi, OpenCode, Claude Code): test yours before turning this on.

⚠️ **Elicitation needs a stateful session.** A stateless surface cannot ask.
Refusing `stateless` together with `confirm_mutating` at lint is not built yet
(planned with stateless sessions, sub-project 4).

⚠️ **Check your client's MCP tool-call timeout.** The gateway waits up to 300 s
for the human; a host that abandons the call sooner shows a failure while the
question may still be open.

`health --deep` probes call the backend directly and bypass the call gates, so a
green probe says nothing about them.

**Order.** Fixed: tool roles, then rate limit (`docs/DEPLOYMENT.md` § Rate
limits), then confirmation. A role refusal spends no rate token; a rate-limited
call asks no human; a declined confirmation spends a token. All three run outside
`call_timeout_s`.

## 6b. Worked example — per-user Plane

Verified end to end against real `plane-mcp-server` 0.3.2 in
[`tests/e2e/`](../tests/e2e/README.md): two callers, two Plane identities, one
gateway.

```toml
[plane]
plugin = "plane-http-apikey"          # NOT `plane`: stdio can never be per-user
  [plane.config]
  base_url = "http://plane-mcp:8211/http/api-key/mcp"
  workspace_slug = "acme"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_API_KEY}"   # attach + probe only
  [plane.identity]
  mode = "client"
    [plane.identity.map]
    authorization = "x-plane-pat"     # each caller sends their own PAT here
  [plane.authz]
  require_roles = ["ai-plane-access"]
```

The caller sends the whole header value — `x-plane-pat: Bearer pat-…` — which is
what a LibreChat `customUserVars` entry can carry unchanged. The gateway stores
nothing.

⚠️ **Which mount, and why it is not the obvious one.** plane-mcp-server serves
`/http` (bearer) and `/http/api-key`. The bearer mount is an **OAuth proxy**: it
only accepts tokens it minted itself and 401s a forwarded one *before Plane is
consulted*, so a forwarded IdP token cannot reach Plane through it. The api-key
mount takes a per-request PAT and calls Plane with it. `registry-lint` refuses
the wrong pairing offline, because each plugin declares only the mode its mount
can honour, and `plane-http` refuses both upstream mounts by path.

### The IdP-token variant: `plane-http` + `contrib/plane-mcp-bearer`

To forward each caller's **own IdP token** instead of a PAT, `plane-http` (mode
`bearer`) needs two things upstream does not provide:

1. A **backend mount that forwards the bearer to Plane.**
   [`contrib/plane-mcp-bearer`](../contrib/plane-mcp-bearer/README.md) is
   upstream's own server behind a verifier that sends a PAT-shaped token as
   `X-Api-Key` (the gateway's deployment credential) and anything else as
   `Authorization: Bearer`. It is pinned to exactly plane-mcp-server 0.3.2,
   because it relies on upstream's private routing.
2. A **Plane that verifies your IdP's tokens**, for example an authentication
   class that checks your realm's JWKS. Plane is the control; the wrapper
   checks only that Plane accepts the token.

```toml
[plane]
plugin = "plane-http"
  [plane.config]
  base_url = "http://plane-mcp-bearer:8211/bearer/mcp"   # REQUIRED: no default
  [plane.env]
  access_token = "${BEHEROUTER_PLANE_ACCESS_TOKEN}"      # a Plane PAT: attach + probe
  [plane.identity]
  mode = "bearer"
  [plane.authz]
  audience = "plane-mcp"
```

Verified in `tests/e2e/`: alice's JWT reaches the Plane API as
`Authorization: Bearer`, and the deployment PAT goes as `x-api-key`.

⚠️ An earlier `plane-http` docstring claimed upstream's `/http` mount verified
the bearer against Plane, and the plugin's default `base_url` pointed there. A
surface built from both attached green and 401'd every user call. The default
is gone: `base_url` is required.

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
| `exchange` on a plugin whose target is not `header` | It forwards an HTTP bearer |
| `exchange` without an http(s) `token_url`, or one with credentials or a fragment | Nowhere safe to post to |
| `exchange` with neither `audience` nor `resource` | It would narrow nothing; that is `bearer` |
| `exchange` without `client_id`, or a `client_secret` that is not exactly one `${VAR}` | An inline secret would be committed with the registry |
| `exchange` with `map`, `key` or `path`; an exchange key on any other mode | It would be silently ignored |
| `lookup` with no `path` and no `$BEHEROUTER_IDENTITY_MAP` | Nowhere to read from |
| A `lookup` map that does not exist, where lint can see it | It would fail every call |
| `require_roles` that is not a non-empty array of names | Malformed gate |
| `audience` that is not a non-empty string or array of strings | Malformed gate |
| `hide_tools` that is not `true` or `false` | Malformed switch |
| `hide_tools` with none of `require_roles`, `audience` or `tools` | Nothing to gate the listing on |
| `tools.<name>.require_roles` that is not a non-empty array of names | Malformed gate (a gated or exempted name the backend does not serve logs a WARNING at attach; lint has no catalogue, and nothing is refused) |
| A per-tool gate with `BEHEROUTER_OIDC_ROLES_CLAIM` unset, or on a `shared`-only gateway | Same as the surface gate: it could never pass |
| `confirm_mutating` that is not `true` or `false`; `confirm_exempt` without `confirm_mutating = true`, or not a non-empty array of names | Malformed switch |
| `[surface.rate_limit]` with an unknown key, or `calls`, `per_s`, `burst` not positive | Malformed limit |

At boot, where the gateway's own environment is authoritative:

| Configuration | Refused because |
|---|---|
| A per-user surface with `BEHEROUTER_AUTH_MODE=shared` | A gateway that cannot verify a user cannot require one |
| `require_roles` or `audience` with `BEHEROUTER_AUTH_MODE=shared` | Same |
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

An `exchange` surface's record also carries
`identity.exchange`: `token_url`, `audience` / `resource` / `scope`,
`client_auth`, and `client_secret: "set" | "unset"` — never the secret and
never a cached token.

An applied identity is logged with the subject, the mode and the **names** of
the material applied — never a value. An exchange logs the surface, subject,
header name and whether the token came from the cache or the IdP.

**The one exception: the audit line.** Every call also writes one JSON line on the
`beherouter.audit` logger (see `docs/DEPLOYMENT.md` § Logs, audit and metrics). It
carries the verified `sub` and may carry the claim values an operator names in
`BEHEROUTER_AUDIT_CLAIMS` (comma-separated, default none) — never a token, header,
credential or exchange material, and never the call's arguments. Claims are read from
the verified JWT only, never from request headers.

```json
{"ts":"2026-10-08T10:12:03.412+02:00","event":"tool_call","call_id":"6f1c…","surface":"plane","tool":"run_tool","inner_tool":"cycle","caller":{"sub":"7d2e…","email":"a@example.com"},"auth":"oidc","outcome":"ok","reason":null,"status":null,"latency_ms":312}
```

A call made with the shared gateway token has no subject: it records
`"caller":{"sub":null}` and `"auth":"shared"`.

### Proving a user's identity reaches the backend

A green `health --deep` probe proves the deployment credential and nothing
more. To prove the per-user path, run the same probe **as a user**:

```bash
beherouter health --deep --surface plane --bearer-file - --json < token.txt
```

```json
{"name": "plane", "probe": "ok",
 "user_probe": {"state": "ok", "subject": "alice",
                "backend_identity": {"id": "…", "email": "alice@bank.invalid"},
                "matches_caller": true}}
```

The token is read from a file or stdin, never from argv. It goes through the
gateway's **own** path: the same verifier (`rejected` with the reason, e.g.
`expired`), the surface's gate and materialisation (`refused`), then the probe
with the caller's identity (`failed`). `matches_caller` compares the identity
the backend returned with the token's `email` / `preferred_username` / `sub`.
`mismatch` is exactly the incident this exists to catch: every user's call
quietly acting as the deployment identity. `rejected`, `refused`, `failed` and
`mismatch` fail the command. A surface without an identity mode reports
`not_applicable`.

On an `exchange` surface the probe exchanges the token exactly as a real call
does, because the exchange runs inside the backend call. A refusal by the IdP
therefore reports as `failed` (with the OAuth error code), not `refused`.

### Expired tokens

An expired token is the typical first incident of a per-user rollout: a client
forwards a stale access token. The gateway:

- logs it at **WARNING** (FastMCP's own line, raised from INFO);
- answers with RFC 6750
  `WWW-Authenticate: Bearer error="invalid_token", error_description="token expired"`
  so a client can tell "refresh" from "re-authenticate". Every other refusal
  keeps the generic answer;
- counts it in `GET /metrics` as
  `beherouter_auth_rejections_total{reason="expired"}`, beside `invalid`,
  `issuer`, `audience` and `scope`.

"Expired" is only ever said about a token whose signature verified. FastMCP
checks the signature before `exp`, so a forged token with an old `exp` counts
as `invalid`.

On Kubernetes, `auth.*` and `identityMap.*` in the chart's values render the
variables above into **both** the Deployment and the `registry-lint` hook Job,
so the pre-deploy gate sees the environment that will actually serve. A pure
`auth.mode=oidc` deployment no longer carries a shared gateway token it would
never check.

⚠️ **Every write is still attributed to one identity unless a surface has a
mode.** Making a surface available to every user did not make it per-user; a
`[surface.identity]` table is what does that.

# Token exchange (RFC 8693) as a fifth identity mode — design

> Status: implemented 2026-10-07. Closes follow-up 2 of
> `2026-09-21-per-user-identity-design.md`.

## Why

`bearer` forwards the caller's own JWT, which works only when the backend
accepts a token addressed to the **gateway**. A backend that verifies `aud`
properly refuses it, and it should. RFC 8693 is the standard way out: the
gateway, authenticated as an OAuth client, trades the caller's verified token at
the IdP for a token addressed to the backend, and forwards that. The backend
sees a token minted for it, carrying the caller's identity, and nothing the
gateway holds can act as any user without one of their tokens in hand.

## Config shape

```toml
[crm]
plugin = "openapi"
  [crm.identity]
  mode = "exchange"
  token_url = "https://idp.example.com/realms/acme/protocol/openid-connect/token"
  audience = "crm-api"                 # string or array; and/or `resource`
  resource = "https://crm.internal/"   # optional; string or array
  scope = "crm.read crm.write"         # optional; string or array (space-joined)
  subject_token_type = "jwt"           # "jwt" (default) | "access_token"
  client_auth = "client_secret_basic"  # default; or "client_secret_post"
  client_id = "beherouter"             # literal or ${VAR}
  client_secret = "${BEHEROUTER_CRM_EXCHANGE_SECRET}"   # MUST be a ${VAR}
  header = "authorization"             # optional, as for `bearer`
  prefix = "Bearer "                   # optional, as for `bearer`
```

Validated offline by `validate_identity` (so by `registry-lint` and at boot):

- `token_url` is required, `http`/`https` with a host, no userinfo, no fragment.
  `http` is allowed because in-cluster IdPs are commonly addressed that way; it
  is the operator's call, exactly as a backend `base_url` is.
- At least one of `audience` / `resource`. An exchange with no target narrows
  nothing; forwarding the caller's own token is `bearer`'s job.
- `client_id` required. `client_secret` required **and must be exactly one
  `${VAR}` placeholder** — an inline secret is refused, because `registry.toml`
  is committed (homelab) or rendered into a ConfigMap (chart).
- `map`, `key`, `path` are refused; the exchange keys are refused on every
  other mode, so a half-edited table cannot carry dead keys.
- Only a plugin whose `IdentitySupport.target` is `header` may declare it.
- The generic rule that a per-user surface needs `BEHEROUTER_AUTH_MODE` `oidc` or
  `both` (boot always, lint where visible) already covers it: `shared` never
  yields a JWT to exchange.

## Which targets — header only

| Backing | Exchange? | Why |
|---|---|---|
| `http` | yes | per-call transport headers; the token is an HTTP credential |
| `inproc` (`openapi`, decorator plugins) | yes | `identity_client` applies per-call headers |
| `stdio` | no | structural: no per-request material ever (unchanged) |
| `cli` | no, for now | the token would have to land in a subprocess env; no CLI plugin declares a bearer-shaped credential, and the executor would need an async settle step it does not have. Revisit when a consumer exists. |
| `native` | no | calendar providers hold a **refresh** token and cache a provider per material; an exchanged access token has no refresh half and would churn that cache per expiry. Mode `lookup` is the right fit there. |

`validate_identity` refuses `exchange` unless the plugin's target is `header`,
and `materialise` re-checks it, so a mis-declared plugin cannot reach a backing
that would ignore the pending exchange.

## Where the network call happens

`materialise` stays synchronous and performs **no I/O** (the surface and health
call it synchronously). For `exchange` it returns a `CallIdentity` with empty
headers and a `pending` coroutine factory; `identity.settle(identity)` runs it
and returns the identity with its headers filled. The two header-applying
executors (`ReconnectingMCPExecutor`, `InprocExecutor`) call `settle` **after**
the backing's guard and before the transport is built, so a call the guard
refuses never costs an exchange. The bound-client executor refuses a pending
identity just as it refuses headers.

Consequences:

- Attach does no exchange (no identity resolution at attach, as before).
- `health --deep --bearer-file` exercises the exchange naturally: it calls
  `policy.authorise(req)` and hands the identity to `executor.run`, which
  settles. An exchange refusal therefore reports as `failed`, not `refused`.

## Failure semantics — no fallback, ever

| Token endpoint answers | Raised | Message names |
|---|---|---|
| 400 `invalid_grant` / `invalid_target` | `AuthError` | the surface and the OAuth error code |
| 400/401 `invalid_client`, `unauthorized_client`, `invalid_request`, `invalid_scope`, `unsupported_grant_type`, anything else 4xx | `UsageError` | the surface and the code (or `unrecognised`) |
| 5xx, 429, network error, timeout | `Unavailable` | the surface and the status or exception type |
| 200 without a usable `access_token`, or a non-bearer `token_type` | `Unavailable` / `UsageError` | the surface |
| client secret variable unset | `Unavailable` | the variable name |

`error_description` is never echoed: it is free text an IdP may fill with
anything, including the token. The OAuth `error` code is echoed only when it is
a plain token (`[A-Za-z0-9_.-]{1,64}`). No path returns the deployment
credential or an un-exchanged header set.

The secret is resolved on the **call path**, not at attach: an unset variable
degrades this surface's calls, like an unreadable identity map, rather than
crash-looping the gateway. `health --deep` reports `client_secret: set|unset`.

## Caching and concurrency

- Key: `sha256(surface \0 subject_token)`. Never the raw token, never logged.
- Lifetime: `expires_in − 30 s`. No `expires_in`, or one under the skew, is not
  cached (every call exchanges) — guessing a lifetime would forward an expired
  token.
- Bounded LRU, 1024 entries per surface.
- Concurrent calls for one key share one in-flight exchange (`asyncio.shield`ed
  task), so a burst of tool calls from one agent turn costs one round trip and a
  cancelled caller does not cancel the others. A failure is not cached.
- One exchanger per surface, held at module level and rebuilt if the policy's
  configuration changes.

## Security notes

- The gateway authenticates to the IdP as itself; the IdP's token-exchange
  policy (who may exchange for which audience) is the control. A misconfigured
  IdP policy is refused there with `invalid_target`, not here.
- Logs carry surface, subject, mode, header **names** and whether the token came
  from cache. Never the subject token, the issued token, the secret, or the IdP's
  error description.
- The issued token is held in memory only, for at most its own lifetime.

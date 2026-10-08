# Per-user calendar consent — design

**Status:** implemented 2026-10-07 (`beherouter calendar-consent`,
`src/beherouter/plugins/calendar/consent.py`). Closes follow-up 3 of
[`2026-09-21-per-user-identity-design.md`](2026-09-21-per-user-identity-design.md):
*"Per-user OAuth for the calendar surfaces — the mechanism lands here (target
`credential`), the consent flows do not."* The mechanism has existed since
2026-09-21: `gcal` and `m365` declare mode `lookup`, the executor builds one
provider per identity behind a bounded cache, and `SecretMap` reads the identity
map on the call path. What was missing is how a refresh token for *one person*
gets into that map without an operator hand-running `nc -l` and `curl` per user.

## The decision: a CLI verb, not a gateway route

Two shapes were on the table.

**A. A CLI loopback flow.** `beherouter calendar-consent <surface> --subject
<claim value>` runs the provider's authorization-code flow with PKCE against a
listener on `127.0.0.1`, exchanges the code, and writes the subject's entry into
the identity map file. It is the procedure in `docs/CALENDAR-BOOTSTRAP.md`,
mechanised, with the output going to the map instead of the vault.

**B. An HTTP consent route on the gateway** (`/<surface>/consent` →
provider → `/<surface>/callback`), the user's own JWT naming the subject.

**A was chosen.** B fails on four constraints that are not preferences:

1. **Network posture.** The gateway is bearer-auth behind a proxy; every route it
   serves answers to `Authorization: Bearer`. A browser redirect from Google or
   Microsoft carries no bearer, so a callback route would be the first
   unauthenticated, state-bearing endpoint on the gateway — plus a Caddy
   exception per surface, plus a public redirect URI registered with each
   provider. The edge's default-deny is the thing the per-surface `not` clauses
   protect; a callback hole is exactly what it exists to prevent.
2. **The gateway holds no writable state.** "State is config-file only"
   (AGENTS.md). A route needs a pending-flow store (state → verifier → subject)
   and a *writer* for the identity map; today the gateway only reads it, and the
   map is typically a vault render or a Kubernetes Secret — a projected,
   read-only file the gateway could not write even if it wanted to.
3. **Consent is a one-time, per-person, human act.** It happens once per user
   per grant, in a browser, by the person whose calendar it is. That is an
   operator task with a human in the loop, not a request path. A CLI keeps it
   out of the process every agent call goes through.
4. **Standalone product.** A gateway route would have to be designed for every
   deployment shape (a single VM, a chart behind an ingress, a homelab Caddy) and
   every IdP. A CLI that writes a file works the same in all of them, and the
   file is the existing, documented integration point (`docs/IDENTITY.md` §5).

What B would have bought — self-service without an operator — is real, and is
the right follow-up *if* a deployment wants it; but it belongs in front of the
gateway (an IdP-integrated portal that writes the same map), not in it. The map
format is the seam either way.

## The flow

```
operator/user ── calendar-consent gcal --subject alice@example.com
   │  resolve the registry entry: plugin ∈ {gcal, m365}, identity.mode == lookup,
   │  identity.map cites refresh_token, map path = --identity-map | identity.path
   │  | $BEHEROUTER_IDENTITY_MAP; refuse a symlinked map; resolve the OAuth client
   │  bind 127.0.0.1:8765 (BEFORE the browser), PKCE S256 + state
   ├─▶ browser: provider authorize URL (+ login_hint when the subject is an email)
   │◀── redirect http://localhost:8765/?code=…&state=…   (constant-time state check)
   ├─▶ token endpoint: authorization_code + code_verifier (+ client_secret for Google)
   │◀── refresh_token                         (never printed, logged or echoed)
   └─▶ identity map: flock sidecar → tomlkit read → upsert subject → mkstemp 0600
       → fsync → os.replace → fsync dir        (gateway picks it up by stat)
```

### Where the token lands

The entry's `[surface.identity.map]` already says which **map name** feeds each
logical credential (`refresh_token = "gcal_refresh_token"`). The verb reads that
and writes under the same name — so it cannot write somewhere the surface does
not read, and one person's entry can hold a Google and a Microsoft token side by
side under two names. If the entry also maps `client_id` / `client_secret` per
user, the client the grant was issued to is written beside the token, because a
refresh token is bound to its client.

### Refusals (all before the browser opens)

| Condition | Why |
|---|---|
| plugin is not `gcal` / `m365` | no consent flow exists for it |
| no `[surface.identity]` mode `lookup` | a grant nothing reads; the "believed per-user, actually shared" state |
| identity map does not cite `refresh_token` | the grant could never reach the provider |
| no map path anywhere | nowhere to write |
| map is a symlink | a projected Secret; replacing it severs the projection |
| map exists but is not valid TOML | rewriting would destroy what we cannot read |
| `client_id` / `client_secret` unresolvable | names the flag that supplies it |
| `--client-secret-file` on `m365` | a personal-account client is public |
| port busy | names `--port` |

Failures after the browser — wrong `state`, `error=access_denied`, a rejected
exchange, a 200 with no `refresh_token` — write **nothing**. Error messages
carry the provider's error *code* only, never the form or the response body.

### The provider traps, inherited rather than re-derived

- **Google:** `access_type=offline` and `prompt=consent` are always sent (no
  refresh token otherwise, silently). The *Production vs Testing* status of the
  consent screen cannot be detected from a token response, so every Google grant
  carries a warning naming the 7-day expiry.
- **Microsoft:** the authorize URL is **derived** from the `TOKEN_URL` constant
  in `providers/microsoft.py` (`consumers`), held by
  `test_token_url_uses_the_consumers_authority` and by
  `test_microsoft_consent_uses_the_consumers_authority`. `offline_access` comes
  from the same `SCOPE` the refresh grant uses. The output reminds the operator
  to prove it past the first refresh.

### The client credentials

Defaulted from the entry's own `env` (`client_id`, and `client_secret` for
Google), resolving its `${VAR}`s — because the grant must be issued to the
*same* client the surface will refresh with. On a workstation without the vault
environment, `--client-id` and `--client-secret-file PATH|-` override; the secret
is never accepted in argv (`ps` shows argv), the same rule as `--bearer-file`.

### Revoke

`--revoke` reads the subject's token, revokes it upstream where the provider has
an endpoint (Google's `oauth2.googleapis.com/revoke`; `400` is reported as
`already_invalid`), and removes the name from the map whatever upstream said —
the operator asked for removal, and a down endpoint must not leave the token
live locally. Microsoft personal accounts have no revocation endpoint; the
output names `account.live.com/consent/Manage`. Only the refresh-token name is
removed: a per-user client id under the same subject may serve another surface.
An entry left empty is dropped.

## What does not change

- **Attach does no identity work**; this module is never imported by `serve`.
- **The map is read on the call path**; `SecretMap` re-parses on a changed
  `(mtime_ns, size)`, and `os.replace` always yields a new mtime. No reader
  change was needed.
- **No identity value is logged.** The verb emits the subject (the operator
  typed it), the key claim, the map path and the *names* written. Never a token,
  a code or a client secret. The loopback handler's access log is silenced,
  because the default one prints `GET /?code=…` to stderr.

## Deployment notes

- **Run it as the gateway's user, against the gateway's map**, on a VM. The
  write preserves the previous owner when run as root, so the gateway's UID can
  still read it.
- ⚠️ **A map rendered by configuration management is overwritten by the next
  run.** On the homelab, a map rendered from the vault is the source of truth;
  write to a working copy (`--identity-map ./map.toml`) and move the value into
  the vault, or stop rendering the map and let the file be the source of truth.
  Pick one — both is a grant that disappears at the next playbook run.
- **Kubernetes:** the map is a projected Secret (a symlink) and is refused.
  Write a working copy and apply it as the Secret; mount the directory, not a
  `subPath`, for the hot reload to work (`docs/IDENTITY.md` §5).
- **Remote host:** the redirect goes to `localhost` *of the browser's machine*.
  Run the verb where the browser is, or `ssh -L 8765:127.0.0.1:8765` and use
  `--no-browser` to print the URL.

## Also fixed alongside: evicted providers leaked their clients

`CalendarExecutor`'s LRU dropped an evicted per-user provider without
`aclose()`, leaking two `httpx.AsyncClient`s (the provider's and its token
refresher's) per eviction. Closing at eviction would close a client a slow call
for that user is still using. The executor now counts calls in flight per
provider (acquire is synchronous with the lookup, so no await can interleave);
eviction **retires** a provider, and a retired provider is closed when its count
reaches zero — immediately if idle, otherwise when its last call finishes. A
failing close is logged by type and never fails the (other user's) call that
triggered it; `aclose()` closes retired providers too.

## Follow-ups left open

1. **Wrong-account consent is not detected.** `login_hint` steers the browser,
   but a user can still sign in as someone else. Detecting it needs an
   `openid email` scope and an id-token check against the subject — or a
   provider identity call after the exchange (`/users/me/calendarList` primary
   id, Graph `/me`). Cheap, but it widens the requested scope; deferred.
2. **No refresh-on-write proof.** The verb does not perform a refresh grant
   before writing (for Microsoft it would rotate the token, and the rotated one
   would then have to be stored instead). `health --deep --bearer-file` as the
   user is the proof, and the output says so.
3. **Self-service portal** (shape B, outside the gateway) if a deployment needs
   users to consent without an operator.

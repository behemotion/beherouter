# Getting the calendar refresh tokens

One-time, per account, by hand. The gateway does not host an OAuth consent flow
— that would mean a login UI, session state and redirect URIs, none of which
belong in a backend router. The output of each procedure is one long-lived
refresh token string that goes into the deployment's secret store. For **per-user**
grants the same consent runs through a CLI verb instead and lands in the
identity map — see § Per-user grants.

**Do this on a machine with a browser, not on the server.** Google
refresh tokens are bound to the OAuth *client id*, not to a machine, so the
resulting string is portable.

## Google (`gcal`)

1. Google Cloud Console → new project → **APIs & Services → Library** → enable
   **Google Calendar API**.
2. **OAuth consent screen** → User type **External** → add your own address as a
   test user.
3. **⚠️ PUBLISH THE APP TO PRODUCTION.** Left in *Testing*, Google expires
   refresh tokens after **7 days** and the surface dies weekly. Publishing
   without Google verification is fine — you are the only user, and you click
   past the "unverified app" warning yourself.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.** A *Web
   application* client will not work with the loopback flow below. Note the
   client id and client secret.
5. Start a listener for the redirect, then consent in a browser:

   ```bash
   nc -l 8765     # leave running; it prints the redirect line containing ?code=...
   ```

   ```
   https://accounts.google.com/o/oauth2/v2/auth
     ?client_id=<CLIENT_ID>
     &redirect_uri=http://localhost:8765
     &response_type=code
     &scope=https://www.googleapis.com/auth/calendar
     &access_type=offline
     &prompt=consent
   ```

   **`access_type=offline` and `prompt=consent` are both required.** Without
   them Google returns an access token and **no refresh token**, and the
   omission is silent.

   The scope is the broad `calendar`, not `calendar.events`: upstream does not
   document which scope `freeBusy` needs, and a 403 there is indistinguishable
   from a revoked token.

6. Exchange the code (URL-decode it first if it contains `%2F`):

   ```bash
   curl -s https://oauth2.googleapis.com/token \
     -d grant_type=authorization_code \
     -d code=<CODE> \
     -d client_id=<CLIENT_ID> \
     -d client_secret=<CLIENT_SECRET> \
     -d redirect_uri=http://localhost:8765
   ```

   The `refresh_token` field of the response is what you store.

## Microsoft (`m365`, a personal outlook.com account)

Register a client under **Entra ID → App registrations → New → Personal
Microsoft accounts only**, with a redirect URI of `http://localhost:8765` of
type *Mobile and desktop applications*. There is **no client secret** — it is a
public client, which is why the `m365` plugin declares only two credentials.

1. Consent, in a browser (with `nc -l 8765` running as above):

   ```
   https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize
     ?client_id=<CLIENT_ID>
     &response_type=code
     &redirect_uri=http://localhost:8765
     &scope=offline_access%20Calendars.ReadWrite
   ```

   **⚠️ `consumers`, not `common`.** A refresh token issued via the `common`
   authority is **rejected at the first refresh** — everything works for about
   an hour and then stops, and the failure looks like a revoked token rather
   than an authority mismatch. This is the single most dangerous step in this
   document, because it does not fail at setup.

   `offline_access` is what makes Microsoft issue a refresh token at all.

2. Exchange the code:

   ```bash
   curl -s https://login.microsoftonline.com/consumers/oauth2/v2.0/token \
     -d grant_type=authorization_code \
     -d code=<CODE> \
     -d client_id=<CLIENT_ID> \
     -d redirect_uri=http://localhost:8765 \
     -d scope="offline_access Calendars.ReadWrite"
   ```

3. **Wait out one token refresh before trusting it.** An hour of working calls
   proves nothing; the `common`-authority failure appears only at the first
   refresh.

## Where the tokens go

Never into `registry.toml`, and never into this repo. Into the deployment's secret
store (the env file `scripts/deploy.sh` passes, the chart's `secret.env`, or your own
vault), surfaced to the gateway as environment variables the registry references as
`${VAR}` placeholders:

```toml
[gcal]
plugin = "gcal"
  [gcal.env]
  client_id = "${BEHEROUTER_GCAL_CLIENT_ID}"
  client_secret = "${BEHEROUTER_GCAL_CLIENT_SECRET}"
  refresh_token = "${BEHEROUTER_GCAL_REFRESH_TOKEN}"

[m365]
plugin = "m365"
  [m365.env]
  client_id = "${BEHEROUTER_M365_CLIENT_ID}"
  refresh_token = "${BEHEROUTER_M365_REFRESH_TOKEN}"
```

`beherouter plugin-config gcal gcal` emits this block, its reverse-proxy clause
and its env lines together, so the three cannot disagree.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty
`${VAR}` raises during `build_surfaces` — i.e. at startup — so it does not yield
a broken surface, it yields a **dead gateway**, `/healthz` included. Entry and
secret ship in the same deploy, or not at all. `beherouter registry-lint`
catches it locally first.

## Per-user grants: `beherouter calendar-consent`

Everything above produces **one** refresh token, the deployment's, and every
call through the surface acts as that account. For each user to act as
themselves, the surface declares mode `lookup` (`docs/IDENTITY.md` §3, §5) and
each user's own refresh token goes into the **identity map**. The client
registration (steps 1–4 above, or the Entra registration) is done once and
shared; the consent is per user, and is a command rather than `nc` + `curl`:

```toml
[gcal]
plugin = "gcal"
  [gcal.env]
  client_id = "${BEHEROUTER_GCAL_CLIENT_ID}"
  client_secret = "${BEHEROUTER_GCAL_CLIENT_SECRET}"
  refresh_token = "${BEHEROUTER_GCAL_REFRESH_TOKEN}"
  [gcal.identity]
  mode = "lookup"
  key = "email"
  path = "/etc/beherouter/identity-map.toml"
    [gcal.identity.map]
    refresh_token = "gcal_refresh_token"
```

```bash
beherouter calendar-consent gcal --subject alice@example.com
#   --no-browser            print the URL instead of opening one
#   --identity-map PATH     write a working copy instead of the entry's path
#   --client-id ID          when the entry's ${VAR}s are not set on this machine
#   --client-secret-file F  (Google only; `-` = stdin; never in argv)
#   --port 8765             the loopback port your OAuth client allows
beherouter calendar-consent gcal --subject alice@example.com --revoke
```

It binds `127.0.0.1:8765`, opens the provider's consent page (PKCE, a `state`
check, `login_hint` = the subject), exchanges the code with the surface's own
OAuth client, and writes the token into the subject's map entry under the name
the entry's `[surface.identity.map]` cites — atomically, `0600`, keeping every
other entry and comment. The gateway picks it up on the next call by `stat`; no
restart. It **never prints the refresh token**. The same traps hold, enforced
rather than remembered: Google always gets `access_type=offline` +
`prompt=consent`, Microsoft always gets the `consumers` authority, and every
Google grant warns about the 7-day Testing expiry, which cannot be detected.

- `--subject` is the value of the entry's `key` claim, exactly as the caller's
  JWT carries it.
- **Run it where the browser is.** On a headless host,
  `ssh -L 8765:127.0.0.1:8765 host` and `--no-browser`.
- ⚠️ **A map rendered by configuration management is overwritten by its next
  run.** Either your secret store renders the map (write a copy with
  `--identity-map` and move the value into the store) or the file on the host is
  the source of truth — not both.
- A **symlinked** map (a projected Kubernetes Secret) is refused: write a copy
  and apply it as the Secret.
- `--revoke` revokes upstream at Google and removes the name locally either
  way; Microsoft personal accounts have no revocation endpoint, so the user
  removes the app at <https://account.live.com/consent/Manage>.
- Prove it as the user: `beherouter health --deep --surface gcal --bearer-file -`
  with their token on stdin — and for `m365`, again after the first refresh.

Design and the rejected alternative (a consent route on the gateway):
`docs/superpowers/specs/2026-10-07-calendar-consent-design.md`.

## Proving it works

```bash
podman exec beherouter beherouter health --deep --json
```

`probe = "list_calendars"` performs a real credentialed call. A revoked or
wrong-authority refresh token shows as `probe: "failed"` while `tools/list` and
`search_tools` stay green — they are served from the catalogue, which for a
native plugin is static and never touches the credential.

## Rotation

Both tokens can be revoked upstream at any time, and nothing in the gateway can
repair that: access tokens are in-memory only and there is no token cache to
refresh from. Rotating means re-running the procedure above and redeploying the
secret. `beherouter health --deep` is the only thing that sees a dead
credential; schedule it with `--textfile` (`contrib/health-textfile/`, or the
chart's `healthCronJob`) so a revoked grant pages instead of waiting for a user.

⚠️ **Microsoft rotates the refresh token on every refresh**, and expects the
replacement to be used next time; Google does not rotate. The gateway keeps a
rotated token **in memory for the life of the process** — deliberately not on
disk, so the no-writable-state property holds — and falls back to the stored
value on restart, which Microsoft still honours for a client that never used a
rotation. The stored value therefore does not go stale by itself, but it is also
never updated by the running gateway: **rotating for real means re-running the
bootstrap and redeploying the secret**, exactly as above.

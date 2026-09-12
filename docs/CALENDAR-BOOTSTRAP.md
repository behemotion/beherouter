# Getting the calendar refresh tokens

One-time, per account, by hand. The gateway does not host an OAuth consent flow
— that would mean a login UI, session state and redirect URIs, none of which
belong in a backend router. The output of each procedure is one long-lived
refresh token string that goes into the deployment's vault.

**Do this on a machine with a browser (the Mac), not on the server.** Google
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

Never into `registry.toml`, and never into this repo. Into the homelab ansible
vault, surfaced to the gateway as environment variables the registry references
as `${VAR}` placeholders:

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

`beherouter plugin-config gcal gcal` emits this block, its Caddy `not` clause
and its `beherouter-env.j2` lines together, so the three cannot disagree.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty
`${VAR}` raises during `build_surfaces` — i.e. at startup — so it does not yield
a broken surface, it yields a **dead gateway**, `/healthz` included. Entry and
secret ship in the same playbook run, or not at all. `beherouter registry-lint`
catches it locally first.

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
refresh from. Rotating means re-running the procedure above and re-running the
playbook. `beherouter health --deep` is the only thing that sees a dead
credential, and it is **not scheduled** — see § Follow-up in the plan.

⚠️ **Microsoft rotates the refresh token on every refresh**, and expects the
replacement to be used next time; Google does not rotate. The gateway keeps a
rotated token **in memory for the life of the process** — deliberately not on
disk, so the no-writable-state property holds — and falls back to the vault's
value on restart, which Microsoft still honours for a client that never used a
rotation. The vault value therefore does not go stale by itself, but it is also
never updated by the running gateway: **rotating for real means re-running the
bootstrap and the playbook**, exactly as above.

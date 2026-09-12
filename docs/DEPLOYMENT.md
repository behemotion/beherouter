# Deploying beherouter

## What you are deploying

One container, one port, no database.

| | |
|---|---|
| **Process** | `beherouter serve --host 0.0.0.0 --port 47100` |
| **State** | `registry.toml` only — config, not data |
| **Persistence** | none. No volume, no database, no writable state |
| **Network posture** | bind loopback; a reverse proxy is the per-client token boundary |

Having no state is what makes rollback cheap, and it is worth preserving.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `BEHEROUTER_GATEWAY_TOKEN` | **yes** | Shared bearer token the gateway itself checks |
| `BEHEROUTER_REGISTRY` | yes | Path to `registry.toml` |
| `BEHEROUTER_PUBLIC_URL` | no | Published origin used by `client-config` output |
| `BEHEROUTER_<SURFACE>_*` | per backend | Backend credentials, referenced from the registry as `${VAR}` |

Secrets appear in `registry.toml` as `${VAR}` placeholders, expanded from the environment
at attach time. **They are never written into the registry**, so it is safe to commit.

## The registry

```toml
[office]
plugin = "office-mcp"

[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "acme"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_TOKEN}"
```

Do not hand-write the surrounding plumbing. `beherouter plugin-config <surface> <plugin>`
emits the registry block, the reverse-proxy clause and the env line **from the plugin's own
spec**, so the three cannot disagree. It never emits a credential, only a placeholder.

See [`PLUGINS.md`](PLUGINS.md) for what a plugin is and how to write one.

## Pre-deploy gate

```bash
beherouter registry-lint --path registry.toml --json
```

Offline: no network, no attach. It turns a production-outage-shaped feedback loop into a
local one.

⚠️ **Run it against the image that is about to serve**, not against a workstation checkout.
Validating inside the new image means it checks the exact code that will run, against the
real environment, so `${VAR}` placeholders are **resolved rather than assumed**. Fail there
and the old container is still running and still serving — the bad registry never reaches
the service.

## Reverse proxy and auth

The gateway binds loopback. The proxy in front of it is what enforces **per-client** tokens,
keeping one client's surface away from another's; the gateway's own token is a single shared
credential and cannot make that distinction.

Configure **default-deny**, with an explicit allow per surface. Publishing the gateway on a
LAN interface would let anyone reach every surface with the one shared token, bypassing that
split entirely.

`/healthz` should be the only unauthenticated path.

## Health and verification

```bash
# transport — is the process up, and what attached at startup?
curl -sS https://<your-gateway>/healthz
# {"status":"ok","surfaces":["office","plane"]}

# the credentials behind each surface — a real call per backend
beherouter health --deep --json      # exit 6 == a backend credential is bad

# the CLI contract, inside the image (not a host venv)
beheaxi conformance "beherouter"     # 6/6
```

⚠️ **An empty surface list is a valid state**, not an outage — it is what a gateway with no
registry entries correctly reports.

⚠️ **A green `/healthz` proves less than it looks like.** It proves the process is up and
which surfaces attached *at startup*. It cannot see a backend credential go bad — a revoked
token lists and searches its catalogue perfectly and fails only on a real call. That is
precisely why `health --deep` exists separately: it makes a **credentialed `tools/call`**
per surface.

`health --deep` also fails a surface whose pin list names a tool the backend no longer
serves (`catalogue: "pinned_missing"`). A pinned name that has disappeared is otherwise
**silently not pinned**, and the only symptom is a short `tools/list`.

## Adding or removing a surface — three edits, not one

| Edit | Miss it and… |
|---|---|
| The `registry.toml` block | — |
| The reverse-proxy allow clause | the surface is unreachable (default-deny) |
| The per-client token in the env | it answers to **another client's token** |

Set `probe` from the start so `health --deep` can prove the credential.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty `${VAR}`
raises during startup, so it does not yield a broken surface — it yields a **dead gateway**,
`/healthz` included. Entry and secret ship together, or not at all.

⚠️ **A stale proxy clause is worse than a missing one.** Remove a surface from the registry
but leave its allow clause, and the retired token still passes the edge — and would
authorize that old token against whatever surface later claims the same path.

## Upgrading

1. Bump the version the image is built from.
2. Rebuild the image.
3. Run the pre-deploy gate **inside the new image**.
4. Restart.
5. Verify `/healthz` **and** `health --deep`.

Ordering matters: build **before** switching config or restarting. A build that fails after
the container has already been restarted against a new registry leaves the gateway
crash-looping on a missing dependency — taking every surface and `/healthz` down.

## Rollback

Rebuild the previous version and restart. There is **no state to migrate**.

```bash
podman logs --tail 200 beherouter        # what actually failed
systemctl --user restart beherouter-compose.service
```

---

## Appendix — a reference deployment

Rootless Podman + a systemd user unit + `loginctl enable-linger` + Caddy, driven by
ansible. Not the only way to run this, but it is the shape these warnings were learned in.

**Reboot survival is observed, not assumed.** With linger enabled the compose units start at
boot with nobody logged in — verify it by rebooting and checking container uptime against
host uptime, rather than trusting the unit file.

### Failure modes worth knowing before you hit them

⚠️ **An attach failure crash-loops the whole gateway**, taking every other surface and
`/healthz` with it. Read the container logs after any registry change.

⚠️ **A same-host backend needs a shared container network.** A rootless bridged container
**cannot reach a port published on its own host** — so co-locating a backend makes it
*harder* to reach, not easier. Loopback, the host-gateway alias, the LAN address and the
public vhost all refuse, while remote hosts answer 200. Join both containers to a shared
network and address the backend by its network alias. Expect older documentation to claim
the opposite.

⚠️ **A Django backend must be addressed by a network alias without underscores.** Django's
host validation permits only `[a-z0-9.-]` plus an optional port and rejects a bad `Host`
with a bare **400 before `ALLOWED_HOSTS` is consulted**. Since podman-compose names
containers with underscores, the obvious value is the broken one.

⚠️ **Beware a frontend with a catch-all route.** Probing an API path against a Next.js
frontend can return 200 where the real API returns 401 — so the surface attaches cleanly and
then returns HTML where the SDK expects JSON. Point at the API, not the web port.

⚠️ **After any rootless-podman major upgrade, run `podman ps`.** A 5.x → 6.x jump can break
rootless podman unless the storage configuration pins the runroot, and the failure is
**latent**: running containers keep running and every health check stays green until the
unit is next cycled, at which point it cannot come back. "The service is up" proves nothing
here.

⚠️ **Build tooling must actually be present.** A slim Python base image has no `git`; an
image that clones its own source at build time must install it.

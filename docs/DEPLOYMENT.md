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

## The image

Published at **`ghcr.io/behemotion/beherouter`** — multi-arch (`linux/amd64`,
`linux/arm64`), one tag per release: git tag `v0.2.0` → image tag `0.2.0`
(semver, no leading `v`). `latest` follows the newest release.

```bash
docker pull ghcr.io/behemotion/beherouter:0.2.0
```

The release workflow builds the image from the repo-root
[`Containerfile`](../Containerfile) on every `v*` tag, after the full test
suite passes on the tagged revision, and refuses a tag that disagrees with the
`pyproject.toml` version or the chart's `appVersion` — the tag, the image and
the chart default cannot drift apart.

Build it yourself only if you need to: a registry mirror, or an extended image
(a stdio backend's binary must be **inside** the image — see the chart's
caveats). The Containerfile builds from a plain checkout of that tag.


## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `BEHEROUTER_GATEWAY_TOKEN` | **yes**, unless `AUTH_MODE=oidc` | Shared bearer token the gateway itself checks |
| `BEHEROUTER_REGISTRY` | yes | Path to `registry.toml` |
| `BEHEROUTER_PUBLIC_URL` | no | Published origin used by `client-config` output |
| `BEHEROUTER_<SURFACE>_*` | per backend | Backend credentials, referenced from the registry as `${VAR}` |

Secrets appear in `registry.toml` as `${VAR}` placeholders, expanded from the environment
at attach time. **They are never written into the registry**, so it is safe to commit.

### Per-user identity (optional)

Unset, none of this applies and the gateway behaves exactly as it always has.
The mechanism, the four modes and the operating notes are in
[`IDENTITY.md`](IDENTITY.md); what a deployment needs to know:

| Variable | Required | Meaning |
|---|---|---|
| `BEHEROUTER_AUTH_MODE` | no (`shared`) | `shared` \| `oidc` \| `both`; `both` accepts a user JWT *beside* the shared token |
| `BEHEROUTER_OIDC_ISSUER` | with `oidc`/`both` | Checked as `iss` |
| `BEHEROUTER_OIDC_AUDIENCE` | with `oidc`/`both` | Checked as `aud` |
| `BEHEROUTER_OIDC_JWKS_URI` | with `oidc`/`both` | Explicit; **never discovered from the issuer** |
| `BEHEROUTER_OIDC_REQUIRED_SCOPES` | no | Comma-separated, gateway-wide |
| `BEHEROUTER_OIDC_ROLES_CLAIM` | with `[surface.authz]` | Dotted path, e.g. `realm_access.roles`; no default |
| `BEHEROUTER_IDENTITY_MAP` | with mode `lookup` | Default path to the mounted secret map |

⚠️ **Two configurations refuse to boot**, which is a dead gateway and `/healthz`
with it, so `registry-lint` checks both wherever these variables are visible: a
surface requiring a verified user while `BEHEROUTER_AUTH_MODE` is `shared`, and
a `require_roles` gate with no `BEHEROUTER_OIDC_ROLES_CLAIM`.

Mount the identity map **read-only**, and note that it is read on the call path
rather than at attach: a bad path degrades that one surface instead of killing
the gateway. `health --deep --json` reports its state per surface.

## The registry

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

1. Get the new image: published releases already exist
   (`ghcr.io/behemotion/beherouter:<version>`); a self-built image is the only
   case where this step means "rebuild".
2. Run the pre-deploy gate **inside the new image**.
3. Restart.
4. Verify `/healthz` **and** `health --deep`.

Ordering matters: build **before** switching config or restarting. A build that fails after
the container has already been restarted against a new registry leaves the gateway
crash-looping on a missing dependency — taking every surface and `/healthz` down.

## Rollback

Rebuild the previous version and restart. There is **no state to migrate**.

```bash
podman logs --tail 200 beherouter        # what actually failed
systemctl --user restart beherouter-compose.service
```

## Deploying on Kubernetes (Helm)

The chart lives at [`charts/beherouter`](../charts/beherouter) — one Deployment,
no state; every rule on this page has a direct translation there, and the chart
README carries the values reference. Dev VMs in this harness are Kubernetes-only,
which is why this path exists.

```bash
helm install beherouter oci://ghcr.io/behemotion/charts/beherouter \
  --namespace beherouter --create-namespace \
  --set-string secret.gatewayToken="$(openssl rand -hex 32)"
```

That is the whole install: **chart and image are both published** — the chart as
an OCI artifact pushed once per release and never overwritten, the image as its
own `appVersion` default (`ghcr.io/behemotion/beherouter:<chart appVersion>`) —
and an empty registry is a valid parked gateway. Installing from a checkout
(`helm install beherouter charts/beherouter`) is equivalent and is what CI
exercises. `prod-values.yaml` then carries the one required value —
`secret.gatewayToken` — plus `registry:` and one `secret.env` key per `${VAR}`
it names; render fails loudly without the token, so an install cannot
half-happen.

How the chart holds this page's rules:

| Rule on this page | What the chart does |
|---|---|
| Pre-deploy gate **inside the image that is about to serve** | a pre-install/pre-upgrade **hook Job**: same image, same env, same registry text; lint failure fails the release before anything is created |
| Entry and secret ship together | `required` at render time — no token, no release; an unset `${VAR}` fails the hook, not a live surface |
| The proxy is the **per-client** token boundary | the Ingress routes only; per-client auth is whatever fronts it (forward-auth, Gateway filters). The shared in-app token cannot make that distinction here either |
| `/healthz` is the only unauthenticated path | startup/liveness/readiness probes all hit it; `helm test` asserts its payload |
| Loopback bind, only the edge reaches in | optional NetworkPolicy: default-deny ingress except the sources you list |
| An attach failure crash-loops the gateway | `maxUnavailable: 0` rolling update — the failing NEW pod stalls the rollout while the **previous revision keeps serving**; `helm rollback` back, no state to migrate |

Upgrade is `helm upgrade` (the lint hook re-runs first); rollback is `helm
rollback`. Deep verification stays the same command, run against the Deployment:

```bash
kubectl exec deploy/beherouter -- beherouter health --deep --json   # exit 6 == bad credential
```

### Materialising a `stdio` backend at pod start (a `cmd` override)

The published image carries the **gateway only**. A `stdio` plugin whose server
is not in the image — `plane`, whose default `cmd` points at a path the homelab
image bakes in — can be materialised at start-up by overriding `cmd` in the
registry entry:

```toml
[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "acme"
  cmd = "uv run --exclude-newer 2026-09-01 --with plane-mcp-server==0.3.2 plane-mcp-server stdio"
```

⚠️ **This trades a build-time dependency for a runtime one.** A PyPI outage or
an egress change then presents as a **crash-looping gateway** rather than a
failed build, because an attach failure takes `/healthz` and every other surface
with it. Baking the server into a derived image is the sturdier choice; what
follows is what the override needs when you take it anyway (all four verified in
production by an operator running it under a hardened security context):

| Requirement | Why |
|---|---|
| `HOME` and `UV_CACHE_DIR` pointed at a writable path — on the chart, `extraEnv` onto the `/tmp` emptyDir it already mounts | with `readOnlyRootFilesystem: true` and neither set, the gateway crash-loops with **no clear error** |
| `--exclude-newer <date>` beside the `==` pin | `plane-mcp-server==0.3.2` pins only the top level; without a date pin its ~80 transitive dependencies re-resolve on **every pod start**, so two pods restarted weeks apart run different code |
| Nothing else on stdout | `uv` writes progress to **stderr only**, which is why this works at all: the MCP stdio channel on stdout stays uncorrupted |
| Accept two FastMCP versions in one pod | `uv run --with` builds an isolated environment, so the backend can hold a different FastMCP than the gateway (3.2.0 beside 3.4.5, observed) — a feature here, not a conflict |

```yaml
# values.yaml — the two variables, onto the emptyDir the chart already mounts
extraEnv:
  - name: HOME
    value: /tmp
  - name: UV_CACHE_DIR
    value: /tmp/uv-cache
```

Under `runAsNonRoot` + UID 1000 + all capabilities dropped +
`automountServiceAccountToken: false`, a full JSON-RPC `initialize` through such
a backend succeeds.

⚠️ A `stdio` backend can never carry a per-request identity, however it is
materialised — see [`IDENTITY.md`](IDENTITY.md) §7.

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

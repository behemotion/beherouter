# DEPLOY — beherouter

> Runbook: what an operator runs today. beherouter has **no first-party
> deployment** — this repo ships public artifacts (an image and a Helm chart),
> and a deployer runs them. The detailed reference, with every failure mode, is
> [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md); this file is the short path through it.

## Shape

| | |
|---|---|
| Artifacts | image `ghcr.io/behemotion/beherouter:<version>` (multi-arch, no leading `v`); chart `oci://ghcr.io/behemotion/charts/beherouter` |
| Host | the deployer's: one podman host (`scripts/deploy.sh`) or a Kubernetes cluster (the chart) |
| Runtime | rootless or rootful Podman, or Kubernetes |
| App | port **47100** (`beherouter serve --host 0.0.0.0 --port 47100` in the container, published on `127.0.0.1:47100`) |
| DB | none — state is `registry.toml` (plus an optional read-only identity map) |
| Edge | a reverse proxy in front, which is the per-client token boundary; the vhost is the deployer's |

Port 47100 is beherouter's slot in the harness port registry
([CONVENTIONS §7](../docs/CONVENTIONS.md#7-service-packaging--deploy)).

## Prerequisites

- **Podman host:** bash, podman, curl. A `registry.toml` and an env file
  (`KEY=value`, **no quoting** — a quoted token is a 401 that looks like a bad token).
  Files the gateway reads must be readable by **UID 1000** (the image's user).
- **Kubernetes:** helm 3 and a value for `secret.gatewayToken` (render refuses without it).
- Every `${VAR}` the registry names must exist **before** the entry ships: an unset
  or empty one is a `UsageError` at startup, i.e. a dead gateway, `/healthz` included.

## Deploy

### Single podman host — `scripts/deploy.sh`

```bash
scripts/deploy.sh --registry /srv/beherouter/registry.toml \
                  --env-file /srv/beherouter/.env --deep      # this checkout's version
scripts/deploy.sh --tag <version> --deep                      # a specific published release
scripts/deploy.sh --build --deep                              # build from this checkout
scripts/deploy.sh --dry-run ...                               # print every command, change nothing
```

Order: pull (or `--build`) → snapshot the registry → `registry-lint` **inside the
new image** with the env it will serve with → stop and keep the old container as
`<name>-previous` → start → verify `/healthz` (and `health --deep` with `--deep`)
→ on any failure, roll back to the previous container **and registry snapshot**.
Exit `0` deployed and verified, `1` failed (rolled back), `2` usage error.

A registry change means re-running `deploy.sh` (the container mounts a read-only
snapshot). `${file:}` secrets and the kill-switch state directory go in with
`--volume SRC:DST[:OPTS]`. Reboot survival: enable `podman-restart.service` once
(details: `docs/DEPLOYMENT.md` § Single-host deploy).

### Kubernetes — the chart

```bash
helm install beherouter oci://ghcr.io/behemotion/charts/beherouter \
  --namespace beherouter --create-namespace \
  --set-string secret.gatewayToken="$(openssl rand -hex 32)"
helm upgrade beherouter oci://ghcr.io/behemotion/charts/beherouter -f prod-values.yaml
```

The `registry-lint` gate is a pre-install/pre-upgrade hook Job (same image, env
and registry); `maxUnavailable: 0` keeps the previous revision serving. Options
worth knowing: `caBundle`, `plugins.install` / `plugins.indexes[]` /
`plugins.local`, `extraInitContainers` / `extraVolumes` / `extraVolumeMounts`, and
the opt-in `healthCronJob` (requires `healthCronJob.textfile.hostPath`). Values
reference: [`charts/beherouter/README.md`](charts/beherouter/README.md).

### Adding or removing a surface — three edits, not one

A `registry.toml` block, its reverse-proxy allow clause, and its token/credential
env line. Registry entry alone → unreachable (default-deny). Registry + env without
the proxy clause → it answers to another client's token. A stale proxy clause is
worse than a missing one. `beherouter plugin-config <surface> <plugin>` emits all
three; `beherouter registry-lint` checks the result offline.

### Publishing a release (maintainer)

Releases are tag-driven; the repo-local `/deploy` skill walks the same steps.

1. `main` clean and equal to `origin/main`; local gates green (`uv run ruff check
   src tests`, `uv run pytest -q`, `uv run beheaxi conformance "beherouter" --json`);
   the latest CI run on `main` green **including the `e2e` job**.
2. Release commit: `pyproject.toml` `version`, `charts/beherouter/Chart.yaml`
   `appVersion` **and** its packaging `version` (any template/values change bumps
   it; a published chart version is never overwritten), `CHANGELOG.md`
   `## [Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`, then `uv lock` (the diff must be
   the beherouter version line only). Patch bump by default; minor/major only on
   the owner's explicit ask.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. `.github/workflows/release.yml`
   runs `check → image → chart`: the full gate set on the tagged revision, the
   tag == `pyproject` == chart `appVersion` == `beherouter --version` gate, the
   image, the GitHub Release (body = the CHANGELOG section), then the chart.
4. Verify: `gh release view vX.Y.Z`; `helm show chart
   oci://ghcr.io/behemotion/charts/beherouter --version <chart version>`; the
   image manifest lists `amd64` and `arm64`.

A failed release: fix on `main`, delete the tag (`git push origin
:refs/tags/vX.Y.Z`, `git tag -d vX.Y.Z`), re-cut; if the chart already published,
the re-cut needs a new chart packaging version.

## Verify

```bash
curl -fsS https://<your-gateway>/healthz       # {"status":"ok","surfaces":[...]}
beherouter health --deep --json                # a real credentialed call per surface; exit 6 = a bad credential
beherouter health --deep --surface S --bearer-file - --json < token.txt   # the same, as a user
helm test beherouter                           # Kubernetes: asserts "status":"ok"
beheaxi conformance "beherouter"               # inside the image: 6/6
```

`/healthz` is HTTP 200 whenever the process is up; `"status": "degraded"` names
`failed` surfaces (and `needs_config_change` for configuration faults no longer
retried). It **cannot see a revoked credential** — schedule `health --deep
--textfile PATH` (`contrib/health-textfile/`, or the chart's `healthCronJob`).
Anything asserting `"status":"ok"` fails on a degraded gateway — intended.

## Rollback

No state to migrate: run the previous image against the previous registry.

```bash
podman logs --tail 200 beherouter
scripts/deploy.sh --tag <previous version> --deep    # podman (also automatic on a failed verify)
helm rollback beherouter                             # Kubernetes
```

## Configuration & secrets

| Env var | Purpose | Where the value lives |
|---|---|---|
| `BEHEROUTER_GATEWAY_TOKEN` | shared bearer the gateway checks (required unless `AUTH_MODE=oidc`) | deployer's secret store → `--env-file` / chart `secret.gatewayToken` |
| `BEHEROUTER_REGISTRY` | path to `registry.toml` | deploy config |
| `BEHEROUTER_PUBLIC_URL` | published origin used by `client-config` | deploy config |
| `BEHEROUTER_<SURFACE>_API_KEY` (and other backend credentials) | referenced from the registry as `${VAR}` or `${file:/path}` | secret store → env / mounted file / chart `secret.env` |
| `BEHEROUTER_AUTH_MODE`, `BEHEROUTER_OIDC_ISSUER`, `_AUDIENCE`, `_JWKS_URI`, `_REQUIRED_SCOPES`, `_ROLES_CLAIM` | inbound identity (`shared` \| `oidc` \| `both`) | deploy config |
| `BEHEROUTER_IDENTITY_MAP` | read-only identity map for `lookup` surfaces | mounted file |
| `BEHEROUTER_ATTACH_TIMEOUT_S`, `BEHEROUTER_CALL_TIMEOUT_S` | attach bound (default 30 s) / per-call bound (unset = none) | deploy config |
| `BEHEROUTER_REGISTRY_WATCH_S`, `BEHEROUTER_RELOAD_DRAIN_S` | registry polling / drain (default 30 s) | deploy config |
| `BEHEROUTER_ADMIN_TOKEN`, `BEHEROUTER_ADMIN_ROLE` | enable the admin API; the token must differ from the gateway token | secret store |
| `BEHEROUTER_KILLSWITCH_PATH` | kill-switch state file (writable directory) | mounted volume |
| `BEHEROUTER_LOG_FORMAT`, `BEHEROUTER_AUDIT`, `BEHEROUTER_AUDIT_CLAIMS` | log shape, audit line on/off, extra audit claims | deploy config |

No secret is ever written into `registry.toml`; it carries only placeholders.
Never name a backend credential `BEHEROUTER_<SURFACE>_TOKEN` — that is the
client's gateway bearer in `client-config` output (`registry-lint` warns).
Full table: `docs/DEPLOYMENT.md` § Configuration.

## Ops

- **Logs:** `podman logs beherouter` / `kubectl logs deploy/beherouter`. One JSON
  audit line per call on `beherouter.audit` (never arguments, never a token).
- **Reload** (re-attaches changed surfaces only): `kill -HUP`, `POST /admin/reload`,
  or `BEHEROUTER_REGISTRY_WATCH_S`. A changed surface whose new app fails keeps
  serving its old one (`/healthz` `reload_failed`); a registry failing lint changes
  nothing (`last_reload`). Gateway-wide env is restart-only.
- **Kill switch / admin API:** routes under `/admin` (listed in `CONNECTIONS.md`
  and `docs/DEPLOYMENT.md` § Admin API). Allow `/admin` at the proxy from the admin
  network only. The kill switch never degrades `status` — alert on `disabled`.
- **Metrics:** `GET /metrics` (unauthenticated, counters only; reset on restart —
  use `rate()`/`increase()`). Series and error `reason` vocabulary:
  `docs/DEPLOYMENT.md` § Logs, audit and metrics.
- **Troubleshooting:** `docs/DEPLOYMENT.md` § Appendix — failure modes worth
  knowing before you hit them.

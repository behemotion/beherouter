# beherouter Helm chart

The Kubernetes packaging of the gateway described in
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md): **one Deployment, no state,
no database.** The registry is a ConfigMap, every credential arrives as an
environment variable expanded at attach time, and `/healthz` and `/metrics`
(counters only) are the only unauthenticated paths.

## Install

The chart runs the **published image** — `ghcr.io/behemotion/beherouter`,
multi-arch, one tag per release (`v0.2.0` git tag → `0.2.0` image tag, which is
also the chart's default). **The only required value is the gateway token.**

```bash
helm install beherouter oci://ghcr.io/behemotion/charts/beherouter \
  --namespace beherouter --create-namespace \
  --set-string secret.gatewayToken="$(openssl rand -hex 32)"
```

The chart is published as an **OCI artifact** beside the image, one push per
release, and a published chart version is never overwritten (the release
workflow refuses it) — so `--version 0.1.3` pins exactly what you tested, and
there is no need to vendor this directory into your own repo. `helm install
charts/beherouter` from a checkout still works and is what CI exercises.

That installs a *parked* gateway (empty registry, `/healthz` answering) — a
valid state. Attach surfaces by adding `registry:` and one `secret.env` key per
`${VAR}` it names. A `values.yaml` beats `--set` for anything multi-line;
`registry:` is a verbatim `registry.toml` string.

```yaml
secret:
  gatewayToken: ""            # REQUIRED -- pass via CD, not the file
  env:
    BEHEROUTER_PLANE_API_KEY: ""   # every ${VAR} the registry references
registry: |
  [office]
  plugin = "office-mcp"
```

### Running your own image instead

Only if you need a registry mirror, or the stdio caveat below (the published
image carries the gateway alone):

```bash
podman build -t registry.example.com/beherouter:0.2.0 .
podman push registry.example.com/beherouter:0.2.0
helm install beherouter charts/beherouter \
  --set image.repository=registry.example.com/beherouter
```

## How the podman deployment maps

| Podman / systemd shape | Chart |
|---|---|
| `registry.toml` on disk | ConfigMap, checksum-annotated so edits roll the pods |
| `.env` rendered from your secret store | Secret (`secret.create=true`) or any existing Secret (`secret.create=false` + `existingSecret.name`) |
| `registry-lint` inside the new image, before switching | **pre-install/pre-upgrade hook Job** running the same image with the same env |
| Caddy edge, per-surface token clauses | Ingress for routing only — **authentication is yours** (forward-auth, oauth2-proxy, Gateway API filters) |
| loopback bind, only the proxy reaches the gateway | `networkPolicy.enabled` (off by default; selectors are cluster-specific) |
| crash-loop takes the whole gateway down | `maxUnavailable: 0` rollout — the old revision keeps serving while the new pod fails |

## The pre-deploy gate is mechanical here

`preDeployLint.enabled: true` (default) renders a hook Job that runs
`beherouter registry-lint` inside the **same image**, against the **same
registry text**, with the **same environment** as the Deployment. This is the
docs' rule ("validate inside the image that is about to serve, so `${VAR}`
placeholders are resolved rather than assumed") made automatic:

- lint fails → **release fails**, nothing is created or replaced;
- lint passes → `helm upgrade` proceeds; the previous revision was never at
  risk.

A failed hook Job is kept for an hour (`ttlSecondsAfterFinished`) so
`kubectl logs job/<release>-registry-lint` shows what the registry got wrong.

If even that gate passes and the pod still crash-loops (an attach failure is a
*runtime* event, e.g. an unreachable backend), the rollout stalls with
`maxUnavailable: 0` — old pods keep serving, `/healthz` stays up — and
`kubectl rollout undo` or `helm rollback` is the way back. There is no state
to migrate.

## Values worth reading before production

| Key | Default | Notes |
|---|---|---|
| `image.repository` / `image.tag` | `ghcr.io/behemotion/beherouter` / chart `appVersion` | Published image; override only for a mirror or a self-built image |
| `secret.create` / `secret.gatewayToken` / `secret.env` | `true` / required / `{}` | Chart-rendered Secret; keys of `env` must match the registry's `${VAR}` names |
| `existingSecret.name` / `existingSecret.gatewayTokenKey` | – | Bring your own Secret (external-secrets, sealed-secrets); backend `${VAR}`s then arrive via `extraEnv` |
| `registry` | `""` | Verbatim `registry.toml`. Empty is a valid parked gateway |
| `publicURL` | auto | `BEHEROUTER_PUBLIC_URL`; derived from the first Ingress host when empty |
| `auth.mode` | `shared` | `shared` \| `oidc` \| `both`. Anything but `shared` needs the `auth.oidc` block; `oidc` drops `BEHEROUTER_GATEWAY_TOKEN` entirely |
| `auth.oidc.issuer` / `.audience` / `.jwksUri` | – | Required together by `oidc`/`both`. The JWKS URI is explicit — never discovered from the issuer |
| `auth.oidc.rolesClaim` | `""` | Dotted path to the roles claim (`realm_access.roles`, `roles`, `groups`). Required by any surface using `[surface.authz]` |
| `identityMap.enabled` / `.secretName` / `.mountPath` | `false` / – / `/etc/beherouter/identity-map.toml` | For identity mode `lookup`. The Secret's **key must be the basename** of `mountPath`; a `subPath` mount does not hot-reload, so rotation needs a pod restart |
| `preDeployLint.enabled` | `true` | The hook gate described above |
| `service.sessionAffinity` | `ClientIP` | MCP sessions are in-process; do not remove if `replicaCount > 1` |
| `networkPolicy.enabled` | `false` | Default-deny ingress except `ingressFrom` sources |
| `replicaCount` / `autoscaling` / `podDisruptionBudget` | `1` / off / off | Replicas are safe but each one re-attaches backends (stdio children included) |
| `securityContext.readOnlyRootFilesystem` | `true` | The gateway writes nothing; `/tmp` is an emptyDir |

## Caveats carried over from the VM deployment

- **The per-client token boundary is not the gateway's — in the default mode.**
  The shared `BEHEROUTER_GATEWAY_TOKEN` is enforced in-app and cannot tell
  clients apart; per-surface, per-client enforcement is an edge concern
  (annotations on the Ingress, or a Gateway with auth filters). Plain Ingress =
  routing only.
- **With `auth.mode: oidc` or `both`, the gateway *can* tell callers apart.** It
  verifies an OIDC JWT against your realm's JWKS, and a surface can then forward
  that caller's own identity to its backend or gate on their roles (see
  `docs/IDENTITY.md`). What that does **not** change: the catalogue and
  `health --deep` still use the deployment credential, so a green probe proves
  the deployment credential and nothing about any user's. `both` keeps the
  shared token working alongside, for clients that cannot mint a JWT.
- **Entry and secret ship together, still.** An unset `${VAR}` still kills the
  pod at boot (by design); here that surfaces as a failed hook or a stuck
  rollout, not a silent broken surface.
- **A stdio backend needs its binary in the image.** The published image ships
  `plane-mcp-server` 0.3.2 at `/opt/plane-mcp/bin/plane-mcp-server`, so the
  in-tree `plane` plugin attaches on its defaults. Any other stdio server must
  be in the image too: extend it, or override the plugin's `cmd`. A missing
  binary is refused at attach **by name**, and `registry-lint` warns about it
  in the hook Job.
- **A same-cluster `http` backend is reached by its Service DNS name**, which
  must be underscore-free for the same Django reason as on podman networks
  (Service names are RFC 1123 anyway).

## Verification

```bash
helm lint charts/beherouter -f charts/beherouter/ci/basic.yaml
helm template charts/beherouter -f charts/beherouter/ci/full.yaml | kubeconform -strict
helm test beherouter            # after install: curls /healthz, asserts the payload
kubectl exec deploy/beherouter -- beherouter health --deep --json   # exit 6 == bad credential
# per-user surfaces: probe AS a user (the token is read from stdin, never argv)
kubectl exec -i deploy/beherouter -- beherouter health --deep --surface plane \
  --bearer-file - --json < user-token.txt
```

`GET /metrics` (unauthenticated, like `/healthz`) exposes
`beherouter_auth_rejections_total{reason}`, where `reason` is one of `expired`,
`invalid`, `issuer`, `audience` or `scope`. The first incident of a per-user
rollout is usually `expired`.

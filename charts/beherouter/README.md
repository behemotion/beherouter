# beherouter Helm chart

The Kubernetes packaging of the gateway described in
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md): **one Deployment, no state,
no database.** The registry is a ConfigMap, every credential arrives as an
environment variable expanded at attach time, and `/healthz` is the only
unauthenticated path.

## Install

```bash
# 1. Build and push the image the chart runs (repo root Containerfile).
podman build -t registry.example.com/beherouter:0.2.0 .
podman push registry.example.com/beherouter:0.2.0

# 2. Install. Two values are required; everything else has a sane default.
helm install beherouter charts/beherouter \
  --namespace beherouter --create-namespace \
  --set image.repository=registry.example.com/beherouter \
  --set-string secret.gatewayToken="$(...)"        # your secret manager's turn
```

A `values.yaml` beats `--set` for anything multi-line; `registry:` is a
verbatim `registry.toml` string.

```yaml
image:
  repository: registry.example.com/beherouter
secret:
  gatewayToken: ""            # REQUIRED -- pass via CD, not the file
  env:
    BEHEROUTER_PLANE_TOKEN: ""   # every ${VAR} the registry references
registry: |
  [office]
  plugin = "office-mcp"
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
| `image.repository` / `image.tag` | required / chart `appVersion` | Build from the repo root Containerfile |
| `secret.create` / `secret.gatewayToken` / `secret.env` | `true` / required / `{}` | Chart-rendered Secret; keys of `env` must match the registry's `${VAR}` names |
| `existingSecret.name` / `existingSecret.gatewayTokenKey` | – | Bring your own Secret (external-secrets, sealed-secrets); backend `${VAR}`s then arrive via `extraEnv` |
| `registry` | `""` | Verbatim `registry.toml`. Empty is a valid parked gateway |
| `publicURL` | auto | `BEHEROUTER_PUBLIC_URL`; derived from the first Ingress host when empty |
| `preDeployLint.enabled` | `true` | The hook gate described above |
| `service.sessionAffinity` | `ClientIP` | MCP sessions are in-process; do not remove if `replicaCount > 1` |
| `networkPolicy.enabled` | `false` | Default-deny ingress except `ingressFrom` sources |
| `replicaCount` / `autoscaling` / `podDisruptionBudget` | `1` / off / off | Replicas are safe but each one re-attaches backends (stdio children included) |
| `securityContext.readOnlyRootFilesystem` | `true` | The gateway writes nothing; `/tmp` is an emptyDir |

## Caveats carried over from the VM deployment

- **The per-client token boundary is not the gateway's.** The shared
  `BEHEROUTER_GATEWAY_TOKEN` is enforced in-app and cannot tell clients apart;
  per-surface, per-client enforcement is an edge concern (annotations on the
  Ingress, or a Gateway with auth filters). Plain Ingress = routing only.
- **Entry and secret ship together, still.** An unset `${VAR}` still kills the
  pod at boot (by design); here that surfaces as a failed hook or a stuck
  rollout, not a silent broken surface.
- **A stdio backend needs its binary in the image.** The repo root Containerfile
  builds the gateway only — the `plane` plugin expects
  `/opt/plane-mcp/bin/plane-mcp-server` inside the image (the deployed VM image
  installs it). Extend the image before attaching stdio surfaces.
- **A same-cluster `http` backend is reached by its Service DNS name**, which
  must be underscore-free for the same Django reason as on podman networks
  (Service names are RFC 1123 anyway).

## Verification

```bash
helm lint charts/beherouter -f charts/beherouter/ci/basic.yaml
helm template charts/beherouter -f charts/beherouter/ci/full.yaml | kubeconform -strict
helm test beherouter            # after install: curls /healthz, asserts the payload
kubectl exec deploy/beherouter -- beherouter health --deep --json   # exit 6 == bad credential
```

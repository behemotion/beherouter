#!/usr/bin/env bash
# The local e2e stack.
#
#   fixtures      fake Plane REST API + the IdP's JWKS          (stdlib python)
#   plane-mcp     plane-mcp-server 0.3.2, HTTP mode             (upstream, real)
#   echo-mcp      an MCP backend that reports its credential    (bearer proof)
#   plane-mcp-bearer  contrib/plane-mcp-bearer over real 0.3.2    (bearer -> Plane)
#   beherouter    the build under test                          (image under test)
#
# One podman network, because the gateway reaches its backends BY NAME — the
# same shape as the homelab's `behe-gateway` network and for the same reason: a
# rootless container cannot reach a port published on its own host.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NET=behe-e2e
GATEWAY_TOKEN="e2e-shared-gateway-token"
DEPLOYMENT_PAT="pat-service-account"
ECHO_TOKEN="e2e-echo-deployment-token"

ISSUER="$(python3 -c "import json;print(json.load(open('$HERE/material.json'))['issuer'])")"
AUDIENCE="$(python3 -c "import json;print(json.load(open('$HERE/material.json'))['audience'])")"
JWKS="$(python3 -c "import json;print(json.dumps(json.load(open('$HERE/material.json'))['jwks']))")"

# ⚠️ Backends must be LISTENING before the gateway starts. An attach failure
# crash-loops the whole gateway (AGENTS.md), so "start everything at once" makes
# a slow backend look like a broken one — which is exactly how this script
# failed the first time it was run from a cold stack.
wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 60); do
    curl -s -o /dev/null --max-time 2 "$url" && return 0
    sleep 1
  done
  echo "$name never came up at $url" >&2
  podman logs --tail 30 "$name" >&2
  exit 1
}

podman rm -f beherouter plane-mcp plane-mcp-bearer echo-mcp e2e-fixtures >/dev/null 2>&1 || true
podman network rm -f "$NET" >/dev/null 2>&1 || true
podman network create "$NET" >/dev/null

echo "== fixtures (fake Plane REST + JWKS) =="
podman run -d --name e2e-fixtures --network "$NET" \
  -v "$HERE/fixtures.py:/app/fixtures.py:ro,Z" \
  -e E2E_JWKS="$JWKS" \
  -e E2E_DEPLOYMENT_TOKEN="$DEPLOYMENT_PAT" \
  -p 18000:8000 \
  python:3.12-slim python /app/fixtures.py >/dev/null

# ⚠️ The OAuth mount refuses a non-HTTPS issuer outright (mcp's
# validate_issuer_url: "Issuer URL must be HTTPS"). This value is only ever
# ADVERTISED in OAuth metadata — the gateway dials the container by name over
# plain HTTP on the shared network — so an https URL that resolves nowhere is
# both valid and honest here.
echo "== plane-mcp-server 0.3.2 (http mode: /http and /http/api-key) =="
podman run -d --name plane-mcp --network "$NET" \
  -e PLANE_BASE_URL="http://e2e-fixtures:8000" \
  -e PLANE_OAUTH_PROVIDER_CLIENT_ID="e2e-client" \
  -e PLANE_OAUTH_PROVIDER_CLIENT_SECRET="e2e-secret" \
  -e PLANE_OAUTH_PROVIDER_BASE_URL="https://plane-mcp.e2e.invalid" \
  -p 18211:8211 \
  localhost/plane-mcp:e2e >/dev/null

echo "== echo-mcp (a backend that accepts a forwarded bearer) =="
podman run -d --name echo-mcp --network "$NET" \
  -v "$HERE/echo_mcp.py:/app/echo_mcp.py:ro,Z" \
  -p 18300:8300 \
  localhost/beherouter:e2e python /app/echo_mcp.py >/dev/null

echo "== plane-mcp-bearer (contrib: forwards a bearer to Plane) =="
# The e2e deployment credential is `pat-…`, not Plane's real `plane_api_…`
# shape, so the PAT pattern is widened to match it.
podman run -d --name plane-mcp-bearer --network "$NET" \
  -e PLANE_BASE_URL="http://e2e-fixtures:8000" \
  -e PLANE_WORKSPACE_SLUG="e2e" \
  -e PLANE_MCP_BEARER_PAT_PATTERN='^pat-' \
  -p 18212:8211 \
  localhost/plane-mcp-bearer:e2e >/dev/null

wait_for e2e-fixtures http://localhost:18000/jwks.json
wait_for plane-mcp-bearer http://localhost:18212/healthz
wait_for plane-mcp http://localhost:18211/http/api-key/mcp
wait_for echo-mcp http://localhost:18300/mcp

echo "== beherouter (the build under test) =="
# Two surfaces, two identity modes:
#   plane  — the caller's own Plane PAT, taken from a client header (`client`),
#            through REAL plane-mcp-server to a REAL Plane REST call.
#   echo   — the caller's own verified JWT, forwarded (`bearer`).
# `plane` also carries a role gate, so the refusal path is exercised too.
cat > "$HERE/registry.toml" <<'TOML'
[plane]
plugin = "plane-http-apikey"
  [plane.config]
  base_url = "http://plane-mcp:8211/http/api-key/mcp"
  workspace_slug = "e2e"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_PAT}"
  [plane.identity]
  mode = "client"
    [plane.identity.map]
    authorization = "x-plane-pat"
  [plane.authz]
  require_roles = ["ai-plane-access"]

[echo]
plugin = "plane-http"
pinned = ["whoami"]
probe = "whoami"
probe_args = {}
  [echo.config]
  base_url = "http://echo-mcp:8300/mcp"
  [echo.env]
  access_token = "${BEHEROUTER_ECHO_ACCESS_TOKEN}"
  [echo.identity]
  mode = "bearer"

[plane-bearer]
plugin = "plane-http"
  [plane-bearer.config]
  base_url = "http://plane-mcp-bearer:8211/bearer/mcp"
  [plane-bearer.env]
  access_token = "${BEHEROUTER_PLANE_PAT}"
  [plane-bearer.identity]
  mode = "bearer"
  [plane-bearer.authz]
  audience = "plane-mcp"

[plane-stdio]
plugin = "plane"
  [plane-stdio.config]
  base_url = "http://e2e-fixtures:8000"
  workspace_slug = "e2e"
  [plane-stdio.env]
  api_key = "${BEHEROUTER_PLANE_PAT}"
TOML

podman run -d --name beherouter --network "$NET" \
  -v "$HERE/registry.toml:/data/registry.toml:ro,Z" \
  -e BEHEROUTER_GATEWAY_TOKEN="$GATEWAY_TOKEN" \
  -e BEHEROUTER_AUTH_MODE="both" \
  -e BEHEROUTER_OIDC_ISSUER="$ISSUER" \
  -e BEHEROUTER_OIDC_AUDIENCE="$AUDIENCE" \
  -e BEHEROUTER_OIDC_JWKS_URI="http://e2e-fixtures:8000/jwks.json" \
  -e BEHEROUTER_OIDC_ROLES_CLAIM="realm_access.roles" \
  -e BEHEROUTER_PLANE_PAT="$DEPLOYMENT_PAT" \
  -e BEHEROUTER_ECHO_ACCESS_TOKEN="$ECHO_TOKEN" \
  -p 47100:47100 \
  localhost/beherouter:e2e >/dev/null

echo "== waiting for /healthz =="
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 http://localhost:47100/healthz >/dev/null 2>&1; then
    curl -s http://localhost:47100/healthz; echo; exit 0
  fi
  sleep 1
done
echo "gateway did not come up; logs:" >&2
podman logs --tail 40 beherouter >&2
exit 1

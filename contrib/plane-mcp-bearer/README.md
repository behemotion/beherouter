# plane-mcp-bearer

Upstream `plane-mcp-server`, plus a mount that forwards the caller's bearer to
Plane. It is what the `plane-http` plugin with identity mode `bearer` needs, and
upstream does not provide.

> ⚠️ **Community-maintained glue, not part of the gateway.** It depends on
> `plane-mcp-server` internals and is pinned to exactly one version (0.3.2). It
> refuses to start against another version until someone re-verifies it.

## Why it exists

`plane-mcp-server` 0.3.2 serves two HTTP mounts, and **neither can carry a
forwarded IdP token**:

| Mount | What it does with `Authorization: Bearer <your IdP JWT>` |
|---|---|
| `/http/mcp` | FastMCP **OAuth proxy**: accepts only tokens it minted itself → **401 before Plane is called** |
| `/http/api-key/mcp` | Treats the credential as a **Plane PAT** (`x-api-key`) → Plane sees an unknown PAT |

So a per-user `plane-http` surface pointed at upstream attaches green and 401s
every user call. `registry-lint` now refuses both upstream mounts for
`plane-http`.

## What it does

It builds the **same** server upstream builds (same tools, same middleware, via
upstream's own `_configured()`), behind a `TokenVerifier` that:

- **Routes a PAT-shaped token to `X-Api-Key`** (`^plane_api_[0-9a-f]{32}\Z` by
  default). That is the gateway's deployment credential, which attaches, lists
  the catalogue and answers `health --deep`.
- **Forwards anything else to Plane as `Authorization: Bearer`**, unchanged.

The only check is that Plane accepts the token on `/api/v1/users/me/`, cached
per token for 60 s. **Plane itself must verify your IdP's tokens**, for example
with an authentication class that checks your realm's JWKS. Plane is the control
here; the wrapper is plumbing.

## Run it

```bash
podman build -t plane-mcp-bearer -f Containerfile .
podman run -d --name plane-mcp-bearer \
  -e PLANE_BASE_URL=http://plane-api:8000 \
  -e PLANE_WORKSPACE_SLUG=acme \
  plane-mcp-bearer
```

| Variable | Default | |
|---|---|---|
| `PLANE_BASE_URL` / `PLANE_INTERNAL_BASE_URL` | — | Plane's API, read exactly as upstream reads them |
| `PLANE_WORKSPACE_SLUG` | — | **required**: an IdP token carries no workspace |
| `PLANE_MCP_BEARER_PATH` | `/bearer` | serves `<path>/mcp` |
| `PLANE_MCP_BEARER_PORT` | `8211` | |
| `PLANE_MCP_BEARER_PAT_PATTERN` | `^plane_api_[0-9a-f]{32}\Z` | what counts as the deployment PAT |
| `PLANE_MCP_BEARER_CACHE_SECONDS` | `60` | how long Plane's verdict on a token is reused |
| `PLANE_MCP_BEARER_ALLOW_UNPINNED` | — | `1` starts against an unverified upstream version |

Point the gateway at it:

```toml
[plane]
plugin = "plane-http"
  [plane.config]
  base_url = "http://plane-mcp-bearer:8211/bearer/mcp"
  [plane.env]
  access_token = "${BEHEROUTER_PLANE_ACCESS_TOKEN}"   # a Plane PAT: attach + probe only
  [plane.identity]
  mode = "bearer"
```

## Upgrading upstream

The wrapper depends on two private details of `plane-mcp-server`:

1. `plane_mcp/client.py` routes `auth_method` `api_key_env` and
   `api_key_header` to `PlaneClient(api_key=)`, and **every other value** to
   `PlaneClient(access_token=)`. The Plane SDK sends the latter as
   `Authorization: Bearer`.
2. `plane_mcp.server._configured()` builds the tool and middleware stack.

Before you bump `UPSTREAM_VERSION`, run `test_plane_mcp_bearer.py` against the
new version. It makes a real upstream `member` call and asserts which header
reached Plane:

```bash
uv run --no-project --with plane-mcp-server==<new> --with pytest \
  --with pytest-asyncio pytest -q
```

Better still would be an upstream mount that forwards the caller's bearer. That
would retire this directory.

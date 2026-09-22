"""Plane over HTTP, per-user by the caller's own PAT — the mount upstream honours.

⚠️ THE MEASUREMENT THAT PUT THIS FILE HERE (plane-mcp-server 0.3.2, 2026-09-22,
against a local stack). The server's two HTTP mounts differ in a way its README
does not spell out, and only one can carry a credential the gateway forwards:

  - `/http` is an **OAuth proxy**. Its verifier checks a token IT minted — a
    FastMCP-issued JWT whose `jti` it resolves in its own store
    (`fastmcp/server/auth/oauth_proxy/proxy.py`) — and 401s anything else
    without ever calling Plane. Attaching there with a forwarded token yields
    `could not attach mcp backend 'plane': Client error '401 Unauthorized'`, and
    the Plane API records NO request. That is the mount `plane-http` targets,
    and it needs a backend that accepts a forwarded bearer; upstream is not it.
  - `/http/api-key` (this one) reads a per-request Plane PAT and
    `x-workspace-slug`, validates the PAT against Plane's own
    `/api/v1/users/me/` as `x-api-key`, and builds the client with it. A PAT
    presented per request therefore resolves to THAT user, in Plane, with that
    user's permissions and attribution.

So: per-user Plane, today, against the published server. The credential is a
per-user PAT rather than an IdP token — a real trade, named rather than hidden:
every caller must hold a PAT, and the gateway never stores one (mode `client`
takes it from the caller's own request and forwards it, nothing is kept).

⚠️ THE PAT RIDES IN `authorization`, NOT IN `x-api-key`. FastMCP's auth
middleware extracts the credential from `Authorization: Bearer` and hands it to
`PlaneHeaderAuthProvider`, which then treats it as the API key; nothing in 0.3.2
reads a literal `x-api-key` request header. The client therefore sends the whole
header VALUE ("Bearer pat-…") in the mapped header, which is also what lets a
LibreChat `customUserVars` entry carry it unchanged.

⚠️ TWO HEADERS PER REQUEST, and that is the point. A PAT plus a workspace header
is the exact shape that ruled out every surveyed aggregator (`docs/DESIGN.md`):
they forward one per-user header, this needs two. `x-workspace-slug` comes from
the attach by default and may be mapped per caller for a multi-workspace
deployment.

The pin list, the probe and the Community-Edition gaps are Plane's, not the
transport's — shared with `plane.py` rather than copied.
"""

from urllib.parse import urlparse

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .plane import PINNED, PROBE, PROBE_ARGS
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

API_KEY_MOUNT = "/http/api-key"

# The two headers the mount reads. A closed set, so a typo'd target is a lint
# error rather than a header the backend silently ignores.
AUTH_HEADER = "authorization"
WORKSPACE_HEADER = "x-workspace-slug"

SPEC = PluginSpec(
    name="plane-http-apikey",
    summary=(
        "Plane work tracking over HTTP, authenticated by a per-request Plane "
        "PAT: the same tools as `plane`, with each caller acting as themselves."
    ),
    backing="http",
    pinned=PINNED,
    probe=PROBE,
    probe_args=PROBE_ARGS,
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default=f"http://plane-mcp:8211{API_KEY_MOUNT}/mcp",
            doc=(
                f"plane-mcp-server's API-KEY mount ('{API_KEY_MOUNT}/mcp'), by "
                f"network alias. The '/http' mount cannot take a forwarded "
                f"credential — see this module's docstring."
            ),
        ),
        ConfigField(
            name="workspace_slug",
            type=str,
            required=True,
            doc=(
                "Plane workspace slug, sent as x-workspace-slug. The mount "
                "refuses any request without it, the attach included."
            ),
        ),
    ),
    env=(
        EnvVar(
            name="api_key",
            doc=(
                "Plane PAT used for ATTACH and the probe only. A per-request "
                "identity overrides it per call; with one configured, no "
                "user's work is ever attributed to this PAT."
            ),
        ),
    ),
    identity=IdentitySupport(
        modes=("client",),
        target="header",
        accepts=(AUTH_HEADER, WORKSPACE_HEADER),
        doc=(
            "The caller's own Plane PAT, taken from a client header and "
            "forwarded verbatim. Only `client`: the mount authenticates a PAT "
            "the caller holds, so there is no token for the gateway to mint "
            "(`bearer`), assert (`claims`) or store (`lookup`)."
        ),
    ),
)


def validate(config: dict) -> None:
    """Refuse a URL that is not the api-key mount. Offline, no I/O.

    The mirror of `plane_http.validate`, and for the same reason: pointing this
    plugin's per-request PAT at the OAuth mount would 401 every call after a
    clean-looking attach — the shape of failure this repo spends its lint budget
    on.
    """
    base_url = config.get("base_url")
    if not base_url:
        return
    path = urlparse(base_url).path.rstrip("/")
    if not path.startswith(API_KEY_MOUNT):
        raise UsageError(
            f"plane-http-apikey: base_url '{base_url}' is not the "
            f"'{API_KEY_MOUNT}' mount. The '/http' mount is an OAuth proxy that "
            f"only accepts tokens it minted itself and 401s a forwarded "
            f"credential; use the `plane-http` plugin for a backend that does "
            f"accept one."
        )
    if not path.endswith("/mcp"):
        raise UsageError(
            f"plane-http-apikey: base_url '{base_url}' is not an MCP endpoint; "
            f"it must end in '/mcp'"
        )


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["base_url"],
            # For an http backing, `env` IS the attach-time header set. The
            # workspace header lives here rather than in the identity map so a
            # single-workspace deployment needs one mapped header, not two;
            # per-call material is merged OVER these, key by key.
            env={
                AUTH_HEADER: f"Bearer {ctx.env['api_key']}",
                WORKSPACE_HEADER: ctx.config["workspace_slug"],
            },
            pinned=ctx.pinned,
        )
    )


register(SPEC, build, validate=validate)

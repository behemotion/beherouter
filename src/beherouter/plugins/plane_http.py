"""Plane over HTTP — the same surface as `plane`, attached so it can be per-user.

WHY A SECOND PLUGIN AND NOT A CONFIG SWITCH ON THE FIRST. `PluginSpec.backing`
is inert, frozen data: `registry-lint`, `plugin-config` and `plugins` read it
without attaching anything, and it is what decides — offline — whether a
`[surface.identity]` table is legal at all. A `backing = "http"` key in a
registry entry would move that decision to attach time, which is precisely the
decision this codebase refuses to make late. Two plugins, one vocabulary: the
pin list and the probe are imported from `plane.py`, not copied.

⚠️ UPSTREAM plane-mcp-server CANNOT SERVE THIS PLUGIN, AND NO DEFAULT URL
PRETENDS OTHERWISE. This plugin forwards the caller's own IdP token as
`Authorization: Bearer`. plane-mcp-server 0.3.2 serves two HTTP mounts and
neither carries one:

  - `/http` is a FastMCP OAuth PROXY. `load_access_token` first calls
    `jwt_issuer.verify_token`, which accepts only a JWT that proxy minted itself
    (a `jti` in its own store), so a forwarded Keycloak/Entra token is 401'd on
    its first line — before Plane is ever called. Measured in `tests/e2e/`
    (2026-09-22) and again by a production deployment (2026-09-24).
  - `/http/api-key` sends the credential downstream as `x-api-key`, i.e. treats
    it as a Plane PAT. A forwarded JWT arrives there as an unknown PAT.
    That mount is `plane-http-apikey`'s, with mode `client`.

An earlier version of this docstring claimed `/http` "verifies the bearer by
calling /api/v1/users/me/ with it". It does not, and the default `base_url`
pointed at it; a surface built from both attached green and 401'd every user
call. `docs/IDENTITY.md` §6b had it right all along.

WHAT THIS PLUGIN NEEDS is a backend that (a) accepts a forwarded bearer and
(b) hands it to Plane as `Authorization: Bearer`, plus a Plane that verifies
your IdP's tokens. For (a)+(b) in front of upstream plane-mcp-server,
`contrib/plane-mcp-bearer/` is a small wrapper built from upstream's own
importable pieces — pinned to exactly one plane-mcp-server version, because it
relies on upstream's private `auth_method` routing (`plane_mcp/client.py`). So
`base_url` is REQUIRED, and `validate` refuses upstream's `/http/mcp` and
`/http/api-key` mounts offline.

⚠️ THE DEPLOYMENT TOKEN IS STILL REQUIRED, and it is not decoration: every
request to the backend is authenticated, `tools/list` included, so the ATTACH
needs a credential of its own. An IdP token would expire under a long-lived
gateway, so against `contrib/plane-mcp-bearer` it is a Plane PAT, which the
wrapper recognises by shape and sends as `X-Api-Key`. It is what lists the catalogue and what `health --deep`
probes with — per-call identity material overrides the header for that one call.
A green probe therefore proves the deployment token and says nothing about any
user's, exactly as `docs/IDENTITY.md` §1 states.

⚠️ THE COMMUNITY-EDITION GAPS AND THE PIN LIST ARE PLANE'S, NOT THE
TRANSPORT'S. `plane.py`'s docstring is the reference for both; changing a pin
here without changing it there is what the shared `PINNED` tuple exists to
prevent.
"""

from urllib.parse import urlparse

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .plane import (
    EDITION_FIELD,
    PINNED,
    PROBE,
    PROBE_ARGS,
    edition_backing_options,
    validate_edition,
)
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

# Upstream plane-mcp-server's two HTTP mounts, spelled once so `validate` can
# refuse both: `/http` + FastMCP's default `/mcp` is the OAuth proxy.
OAUTH_PROXY_MOUNT = "/http/mcp"
API_KEY_MOUNT = "/http/api-key"

SPEC = PluginSpec(
    name="plane-http",
    summary=(
        "Plane work tracking over HTTP: the same tools as `plane`, attachable "
        "with a per-request caller identity."
    ),
    backing="http",
    pinned=PINNED,
    probe=PROBE,
    probe_args=PROBE_ARGS,
    config=(
        ConfigField(
            name="base_url",
            type=str,
            required=True,
            doc=(
                "MCP endpoint of a backend that forwards the caller's bearer to "
                "Plane, e.g. contrib/plane-mcp-bearer's "
                "'http://plane-mcp-bearer:8211/bearer/mcp'. Upstream "
                "plane-mcp-server's own mounts cannot."
            ),
        ),
        EDITION_FIELD,
    ),
    env=(
        EnvVar(
            name="access_token",
            doc=(
                "Deployment credential for ATTACH and the probe only; sent as "
                "Authorization: Bearer. Against contrib/plane-mcp-bearer, a "
                "Plane PAT. A per-request identity overrides it per call. NOT "
                "named `token`: that would make the backend variable "
                "BEHEROUTER_<SURFACE>_TOKEN, the name reserved for the client's "
                "own gateway bearer."
            ),
        ),
    ),
    identity=IdentitySupport(
        modes=("bearer",),
        target="header",
        doc=(
            "Forwards the caller's own verified token to Plane, which validates "
            "it itself. Only `bearer`: Plane authenticates the token rather than "
            "trusting an asserted identity, so `claims` would be a header it "
            "ignores and `client`/`lookup` would reintroduce a stored secret."
        ),
    ),
)


def validate(config: dict) -> None:
    """Refuse upstream's mounts, and any URL that is not an MCP endpoint.

    Every failure below attaches *cleanly* and misbehaves later, which is the
    class of mistake this repo spends its lint budget on: the api-key mount
    treats the forwarded token as a PAT, the OAuth proxy 401s it before Plane
    is consulted, and a non-`/mcp` path 404s only when the first tool is called.
    """
    validate_edition("plane-http", config)
    base_url = config.get("base_url")
    if not base_url:
        return
    path = urlparse(base_url).path.rstrip("/")
    if path.startswith(API_KEY_MOUNT) or f"{API_KEY_MOUNT}/" in f"{path}/":
        raise UsageError(
            f"plane-http: base_url points at '{API_KEY_MOUNT}', which takes a "
            f"per-request Plane PAT as x-api-key, not a bearer. For per-user "
            f"PATs use the `plane-http-apikey` plugin with identity mode "
            f"'client'; to forward an IdP token, point at a bearer-forwarding "
            f"backend such as contrib/plane-mcp-bearer."
        )
    if path.endswith(OAUTH_PROXY_MOUNT):
        raise UsageError(
            f"plane-http: base_url points at '{OAUTH_PROXY_MOUNT}', upstream "
            f"plane-mcp-server's OAuth proxy. It accepts only tokens it minted "
            f"itself and 401s a forwarded one before Plane is called, so every "
            f"user call would fail after a green attach. Point at a "
            f"bearer-forwarding backend instead — contrib/plane-mcp-bearer "
            f"serves one at '/bearer/mcp'."
        )
    if not path.endswith("/mcp"):
        raise UsageError(
            f"plane-http: base_url '{base_url}' is not an MCP endpoint; it must "
            f"end in '/mcp'"
        )


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["base_url"],
            # For an http backing, `env` IS the attach-time header set
            # (`backends/mcp.py: build_transport`), and per-request identity
            # material is merged OVER it for a single call. Lowercase because a
            # per-call `authorization` must replace this one rather than ride
            # beside it as a second header.
            env={"authorization": f"Bearer {ctx.env['access_token']}"},
            pinned=ctx.pinned,
            **edition_backing_options(ctx.config),
        )
    )


register(SPEC, build, validate=validate)

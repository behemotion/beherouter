"""Plane over HTTP — the same surface as `plane`, attached so it can be per-user.

WHY A SECOND PLUGIN AND NOT A CONFIG SWITCH ON THE FIRST. `PluginSpec.backing`
is inert, frozen data: `registry-lint`, `plugin-config` and `plugins` read it
without attaching anything, and it is what decides — offline — whether a
`[surface.identity]` table is legal at all. A `backing = "http"` key in a
registry entry would move that decision to attach time, which is precisely the
decision this codebase refuses to make late. Two plugins, one vocabulary: the
pin list and the probe are imported from `plane.py`, not copied.

⚠️ THE URL IS THE `/http` MOUNT, NEVER `/http/api-key`. plane-mcp-server serves
both (`__main__.py` mounts `<prefix>/http/api-key` before `<prefix>/http`) and
they are different auth schemes:

  - `/http/api-key` takes a per-request `x-api-key` + `x-workspace-slug`. That
    is a per-request PLANE PAT — a second shared-secret scheme with more secrets
    in it, not an identity. A bearer sent there is ignored, so a surface
    configured per-user would attribute every write to whatever PAT the client
    sent. `validate` refuses that URL for exactly this reason.
  - `/http` verifies the bearer by calling `GET <PLANE_BASE_URL>/api/v1/users/me/`
    with it and builds `PlaneClient(access_token=...)`, so the token continues to
    Plane as `Authorization: Bearer`. THIS is the mount that makes a caller act
    as themselves.

⚠️ NO `workspace_slug` CONFIG, unlike the stdio plugin. On this mount the
workspace comes from the verified token's own app installation
(`plane_mcp/client.py` reads `workspace_slug` out of the AccessToken claims), so
a configured one would be a value nothing reads.

⚠️ THE DEPLOYMENT TOKEN IS STILL REQUIRED, and it is not decoration: every
request to `/http` is authenticated, `tools/list` included, so the ATTACH needs
a credential of its own. It is what lists the catalogue and what `health --deep`
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
from .plane import PINNED, PROBE, PROBE_ARGS
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

# The mount, spelled once. `/http` + FastMCP's own default `/mcp` path.
BEARER_MOUNT = "/http/mcp"
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
            default=f"http://plane-mcp:8211{BEARER_MOUNT}",
            doc=(
                "plane-mcp-server's BEARER mount, by network alias. Must be the "
                f"'{BEARER_MOUNT}' endpoint — never '{API_KEY_MOUNT}/mcp'."
            ),
        ),
    ),
    env=(
        EnvVar(
            name="access_token",
            doc=(
                "Plane access token used for ATTACH and the probe only; sent as "
                "Authorization: Bearer. A per-request identity overrides it per "
                "call. NOT named `token`: that would make the backend variable "
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
    """Refuse a URL that is not the bearer mount. Offline, no I/O.

    Both failures below attach *cleanly* and misbehave later, which is the class
    of mistake this repo spends its lint budget on: the api-key mount answers
    every call with the PAT the client sent (or none), and a non-`/mcp` path
    404s only when the first tool is called.
    """
    base_url = config.get("base_url")
    if not base_url:
        return
    path = urlparse(base_url).path.rstrip("/")
    if path.startswith(API_KEY_MOUNT) or f"{API_KEY_MOUNT}/" in f"{path}/":
        raise UsageError(
            f"plane-http: base_url points at '{API_KEY_MOUNT}', which takes a "
            f"per-request x-api-key (a Plane PAT), not a bearer. A per-user "
            f"surface there would attribute every write to that PAT. Use the "
            f"'{BEARER_MOUNT}' mount."
        )
    if not path.endswith("/mcp"):
        raise UsageError(
            f"plane-http: base_url '{base_url}' is not an MCP endpoint; it must "
            f"end in '/mcp' (the bearer mount is '{BEARER_MOUNT}')"
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
        )
    )


register(SPEC, build, validate=validate)

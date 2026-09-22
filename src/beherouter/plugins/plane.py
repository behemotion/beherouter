"""Plane — self-hosted work tracking, run as a stdio subprocess.

STDIO, NOT HTTP, and that is the cheap choice on this host rather than the
obvious one. An http backing would need its own container; the service VM had
~460 MB free with 1.9 GB of swap already in use (measured 2026-09-08), so a
subprocess costs a process instead of a container. It also sidesteps
plane-mcp-server's remote transports, which 401 an un-credentialed probe —
LibreChat reads that 401 as "this server wants OAuth" and the connection sticks
on "Needs Auth" forever.

⚠️ base_url POINTS AT THE API CONTAINER BY ITS NETWORK ALIAS, not at Plane's web
port and not at the public vhost. Three reasons, each verified 2026-09-08:
  1. A rootless bridged container cannot reach a port published on its own host,
     so 127.0.0.1, the LAN IP and the public vhost are all refused from in here.
  2. plane_web_1 is a Next.js frontend with a catch-all route: probing
     /api/v1/users/me/ WITHOUT credentials returns 200, while the real API
     returns 401. Pointing at the web port would attach cleanly and then return
     HTML where the SDK expects JSON.
  3. Going out through Caddy would put the edge's TLS and default-deny in the
     path of every internal call for no benefit.

⚠️ THE PINNED 11, out of the 30 the backend advertises. Two tools were pulled on
the day they shipped, for two different reasons, and BOTH listed cleanly first:
  - `page` — this Plane is the COMMUNITY EDITION and its REST endpoint 404s.
    So do work_log, milestone, workitem_type and initiative. An edition
    boundary, not a permission or a project setting.
  - `get_pql_reference` — uncallable upstream in 0.3.2: its published schema
    declares only `detail` while its dispatcher demands an `action` the schema
    does not permit, so no argument set succeeds.
THE REUSABLE COROLLARY: a backend's catalogue advertises the COMMERCIAL surface,
so "the tool exists" says nothing about whether this deployment serves it. Probe
before pinning; re-probe the whole list after an upgrade or an edition change.

⚠️ EVERY WRITE IS ATTRIBUTED TO ONE PLANE IDENTITY — the PAT minted as
"beherouter-mcp". One shared identity per surface, exactly like any other
backend credential here. THIS IS A PROPERTY OF THE STDIO ATTACHMENT, not of
Plane: a stdio subprocess environment is fixed at spawn and `keep_alive=True`
reuses it across callers, so no per-request credential can reach it. Attach the
same surface with the `plane-http` plugin and a `[surface.identity]` table to
make each caller act as themselves.
"""

from urllib.parse import urlparse

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .spec import ConfigField, EnvVar, PluginContext, PluginSpec

# THE VOCABULARY, shared with `plane-http` rather than copied into it. The two
# plugins are one surface attached two ways; an agent's tool list must not
# depend on which attachment an operator chose, and a pin list maintained twice
# would drift on the first upgrade.
PINNED = (
    "workitem",
    "workitem_comment",
    "workitem_attachment",
    "project",
    "state",
    "member",
    "label",
    "cycle",
    "module",
    "workitem_link",
    "intake",
)

# member/me authenticates against Plane with the real credential and takes no
# other argument, so `health --deep` proves the CREDENTIAL rather than the
# catalogue. This is the check gitea-home lacked: a revoked token lists and
# searches perfectly and fails only on a real call.
PROBE = "member"
PROBE_ARGS = {"action": "me"}

SPEC = PluginSpec(
    name="plane",
    summary="Plane work tracking: work items, cycles, modules, comments and attachments.",
    backing="stdio",
    pinned=PINNED,
    probe=PROBE,
    probe_args=PROBE_ARGS,
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default="http://plane-api:8000",
            doc="Plane REST API, by behe-gateway network alias. Host must not contain '_'.",
        ),
        ConfigField(
            name="workspace_slug",
            type=str,
            required=True,
            doc="Plane workspace slug, e.g. 'homelab'.",
        ),
        ConfigField(
            name="cmd",
            type=str,
            default="/opt/plane-mcp/bin/plane-mcp-server stdio",
            doc="plane-mcp-server entry point inside the gateway image.",
        ),
    ),
    env=(
        EnvVar(
            name="api_key",
            doc="Plane personal access token; handed to the server as PLANE_API_KEY.",
        ),
    ),
)


def validate(config: dict) -> None:
    """Reject a base_url whose HOST contains an underscore.

    Django's host_validation_re permits only [a-z0-9.-] plus an optional :port,
    and it rejects a bad Host with a bare 400 BEFORE ALLOWED_HOSTS is consulted.
    Measured 2026-09-08 from inside the gateway container:
        Host: plane_api_1:8000 -> 400 HTML
        Host: localhost        -> 401 JSON
    podman-compose names the container `plane_api_1`, so the obvious value is
    the broken one; the alias `plane-api` is declared on the api service.
    """
    base_url = config.get("base_url")
    if not base_url:
        return
    host = urlparse(base_url).hostname or ""
    if "_" in host:
        raise UsageError(
            f"plane: base_url host '{host}' contains an underscore; Django "
            f"rejects such a Host header with a bare 400 before ALLOWED_HOSTS "
            f"is consulted. Use the network alias (e.g. 'plane-api'), not the "
            f"podman-compose container name."
        )


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="stdio",
            cmd=ctx.config["cmd"],
            env={
                "PLANE_BASE_URL": ctx.config["base_url"],
                "PLANE_WORKSPACE_SLUG": ctx.config["workspace_slug"],
                "PLANE_API_KEY": ctx.env["api_key"],
            },
            pinned=ctx.pinned,
        )
    )


register(SPEC, build, validate=validate)

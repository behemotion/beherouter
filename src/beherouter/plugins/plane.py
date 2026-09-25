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

⚠️ A PINNED TOOL CAN STILL HALF-WORK. `workitem` is served on Community Edition,
but two of its argument shapes are not (measured by a production deployment on
Plane CE v1.4.1, 2026-09-24):
  - `list` WITHOUT `project_id` goes to the workspace-wide endpoint, which 404s.
  - ANY `pql` is refused with a 400, which plane-mcp-server rewrites as "fix
    your PQL" — so a model retries with different PQL, forever.
`count` hits both at once: it ALWAYS calls the workspace endpoint and turns a
`project_id` into PQL (`tools/workitem.py: _scoped_pql`). One real conversation
made twelve such calls, got twelve 404s, and the model told its user the
service was having a temporary problem. With `edition = "community"` (the
default) these calls are REFUSED here with a sentence naming the fix, and the
tool's description says so up front; `edition = "commercial"` turns both off.

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

# Words agents type that Plane's own descriptions do not use (Plane says
# `cycle`, agents say "sprint"). Shared by all three Plane plugins, like PINNED.
# Rule for adding one: a word an agent would type that the tool's description
# does not already contain -- measured by tests/test_search_eval.py.
SEARCH_ALIASES = {
    "workitem": ("issue", "ticket", "task", "epic", "bug", "story"),
    "cycle": ("sprint", "iteration"),
    "state": ("status", "workflow", "column", "done"),
    "member": ("user", "people", "team", "assignee", "current user"),
    "intake": ("triage", "inbox"),
    "work_log": ("timesheet", "hours", "time tracking"),
    "workitem_comment": ("comment", "reply", "discussion", "add comment"),
    "workitem_relation": ("blocked", "blocking", "dependency", "duplicate"),
    "workitem_activity": ("history", "audit", "changes"),
    "workitem_property": ("custom field",),
    "page": ("wiki", "document", "docs"),
    "label": ("tag",),
    "workitem_type": ("issue type",),
    "release": ("version",),
}

EDITIONS = ("community", "commercial")

# Shared by all three Plane plugins, like PINNED: the edition is a property of
# the Plane behind the surface, not of how the surface is attached.
EDITION_FIELD = ConfigField(
    name="edition",
    type=str,
    default="community",
    doc=(
        "Plane edition behind the surface: 'community' refuses the `workitem` "
        "calls Community Edition cannot serve (workspace-wide list, PQL); "
        "'commercial' forwards them."
    ),
)

_NOT_TRANSIENT = (
    "This is a limit of Plane Community Edition, not a temporary error: "
    "retrying, or rewording the query, fails the same way."
)

COMMUNITY_NOTES = {
    "workitem": (
        "On this Plane (Community Edition): `list` requires `project_id` (find "
        "it with the `project` tool), `pql` is not supported, and `count` works "
        "only with neither `project_id` nor `pql`."
    ),
}


def community_edition_guard(verb: str, args: dict) -> None:
    """Refuse the `workitem` shapes Community Edition cannot serve.

    Refusing at the gateway turns an opaque 404/400 into a UsageError the model
    can act on. Only MEASURED failures are refused (see the module docstring),
    plus `count`, whose code path provably reaches one of them.
    """
    if verb != "workitem":
        return
    action = args.get("action")
    if args.get("pql"):
        raise UsageError(
            f"plane: `workitem` does not accept `pql` here. {_NOT_TRANSIENT} "
            f"Call `workitem` with action 'list' and a `project_id` instead, "
            f"and filter the results yourself."
        )
    if action == "list" and not args.get("project_id"):
        raise UsageError(
            f"plane: `workitem` action 'list' requires `project_id` here; the "
            f"workspace-wide listing does not exist. {_NOT_TRANSIENT} Call "
            f"`project` with action 'list' to find the project id, then list "
            f"its work items."
        )
    if action == "count" and args.get("project_id"):
        raise UsageError(
            f"plane: `workitem` action 'count' cannot be scoped to a project "
            f"here (the server turns `project_id` into PQL). {_NOT_TRANSIENT} "
            f"Use action 'list' with the `project_id` and count the results."
        )


def validate_edition(plugin: str, config: dict) -> None:
    edition = config.get("edition")
    if edition is not None and edition not in EDITIONS:
        raise UsageError(
            f"{plugin}: edition must be one of {EDITIONS}, got {edition!r}"
        )


def edition_backing_options(config: dict) -> dict:
    """The McpBacking keywords an edition implies — `guard` and `notes`."""
    if config.get("edition", "community") == "community":
        return {"guard": community_edition_guard, "notes": dict(COMMUNITY_NOTES)}
    return {}

SPEC = PluginSpec(
    name="plane",
    summary="Plane work tracking: work items, cycles, modules, comments and attachments.",
    backing="stdio",
    pinned=PINNED,
    probe=PROBE,
    probe_args=PROBE_ARGS,
    search_aliases=SEARCH_ALIASES,
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
            doc=(
                "plane-mcp-server entry point. The published image ships it at "
                "this path; override it only for an image that does not."
            ),
        ),
        EDITION_FIELD,
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
    validate_edition("plane", config)
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
            **edition_backing_options(ctx.config),
        )
    )


register(SPEC, build, validate=validate)

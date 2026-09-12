"""office-mcp — the agent file toolbox, reached over the shared podman network.

⚠️ THIS SURFACE IS A PASS-THROUGH AND SAVES NO CONTEXT. office-mcp already does
its own pinned-few + lexical-search split internally (discover/invoke over a
34-tool catalogue), so all four of its tools are pinned rather than re-indexed;
stacking two search layers would buy nothing. The benefit of fronting it is
credential centralisation and one uniform client surface.

⚠️ `base_url` addresses the container over the shared `behe-gateway` network,
NOT via 127.0.0.1 and NOT via the public vhost. A rootless bridged container
cannot reach a port published on its own host, so co-location makes a backend
HARDER to reach, not easier.

No credential: office-mcp has no app-level auth of its own, and the gateway does
not traverse its Caddy vhost, so there is nothing to present.
"""

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from . import register
from .spec import ConfigField, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="office-mcp",
    summary="Agent file toolbox: convert, inspect and edit documents, images, audio and video.",
    backing="http",
    pinned=("discover", "invoke", "file_from_url", "job_status"),
    # None of office-mcp's four tools takes zero arguments, so a bare probe name
    # could not authenticate — which is why probe_args exists at all.
    probe="discover",
    probe_args={"query": "pdf"},
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default="http://office-mcp:8100/mcp/",
            doc="MCP endpoint on the shared behe-gateway network.",
        ),
    ),
)


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["base_url"],
            pinned=ctx.pinned,
        )
    )


register(SPEC, build)

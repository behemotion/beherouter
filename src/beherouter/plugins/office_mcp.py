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
from .spec import ConfigField, IdentitySupport, PluginContext, PluginSpec

# Words agents type for the CAPABILITY they want. office-mcp keeps its 34-tool
# catalogue behind `discover`, so the gateway indexes only the four entry
# points, and "resize an image" matches none of their descriptions. These words
# are the hidden catalogue's own vocabulary (its categories and tool names, from
# office-mcp's src/office_mcp/tools/), aimed at the door to it. Measured by
# tests/test_search_eval.py (tests/search_eval/queries/office-mcp.toml).
SEARCH_ALIASES = {
    "discover": (
        "available tools", "capabilities", "convert",
        "image", "resize", "video", "audio", "pdf", "merge", "ocr",
        "screenshot", "docx", "word", "markdown",
    ),
    "invoke": ("call", "execute", "run tool", "argument"),
    "job_status": ("finished", "progress", "complete", "running", "wait", "result"),
}

SPEC = PluginSpec(
    name="office-mcp",
    summary="Agent file toolbox: convert, inspect and edit documents, images, audio and video.",
    backing="http",
    pinned=("discover", "invoke", "file_from_url", "job_status"),
    # None of office-mcp's four tools takes zero arguments, so a bare probe name
    # could not authenticate — which is why probe_args exists at all.
    probe="discover",
    probe_args={"query": "pdf"},
    search_aliases=SEARCH_ALIASES,
    # ⚠️ `catalogued`, not `verified`: office-mcp was proven live (an agent
    # calling `discover` through the production gateway, 2026-08-04), but no
    # in-tree e2e test attaches it, and a tier is a claim about evidence in
    # the tree. Raising it needs an office-mcp surface in tests/e2e/.
    maturity="catalogued",
    evidence=("tests/search_eval/catalogues/office-mcp-0.1.0.json",),
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default="http://office-mcp:8100/mcp/",
            doc="MCP endpoint on the shared behe-gateway network.",
        ),
    ),
    identity=IdentitySupport(
        modes=("bearer", "claims", "client", "exchange"),
        target="header",
        doc=(
            "office-mcp has no app-level auth of its own, so identity here is "
            "an assertion the backend may act on, not a credential it checks."
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

"""mcp-http: any MCP server over streamable HTTP, attached by URL.

The generic counterpart of the curated `http` plugins. `url` left
`registry.toml` because `gitea-home` attached with no probe and served a dead
token for a day; this plugin brings the capability back WITHOUT that hole:

- `probe` and `pinned` are REQUIRED (`requires_entry`). There is no tested
  default to fall back to, so the operator's entry has to supply the check that
  proves the credential — a surface that only lists tools proves nothing.
- The credential is OPTIONAL (`EnvVar(required=False)`): a backend may have no
  app-level auth (office-mcp does not). When given, it travels as
  `<auth_header>: <auth_prefix><api_key>` on attach, the probe and every
  shared call.
- ⚠️ The credential's logical name is `api_key`, not `token`: `token` would
  derive `BEHEROUTER_<SURFACE>_TOKEN`, the name `client-config` gives the
  client's GATEWAY bearer — two secrets under one name.

`beherouter plugins` marks it `generic`, so no one mistakes it for a curated
plugin. When a backend's catalogue warrants versioned pins, search aliases and
a probe that travels with the code, write a curated plugin instead.
"""

from urllib.parse import urlparse

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="mcp-http",
    summary=(
        "Generic: any MCP server over streamable HTTP, by URL. Needs `probe` "
        "and `pinned` in the entry; the credential is optional."
    ),
    backing="http",
    requires_entry=("probe", "pinned"),
    config=(
        ConfigField("url", str, required=True, doc="the server's MCP endpoint, http(s)"),
        ConfigField("auth_header", str, default="authorization",
                    doc="header carrying `api_key`, when one is set"),
        ConfigField("auth_prefix", str, default="Bearer ", doc="prefix before `api_key`"),
    ),
    env=(
        EnvVar(
            "api_key",
            doc="deployment credential for attach, the probe and shared callers",
            required=False,
        ),
    ),
    identity=IdentitySupport(
        modes=("bearer", "claims", "client", "exchange"),
        target="header",
        doc="the caller's material lands as request headers, over the deployment header",
    ),
)


def validate(config: dict) -> None:
    """Offline: refuse a URL that cannot be an HTTP MCP endpoint."""
    url = config.get("url") or ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"mcp-http: url must be an http(s) URL with a host, got {url!r}")
    if not config.get("auth_header"):
        raise UsageError("mcp-http: auth_header must not be empty")


async def build(ctx: PluginContext):
    key = ctx.env.get("api_key")
    headers = (
        {ctx.config["auth_header"]: f"{ctx.config['auth_prefix']}{key}"} if key else None
    )
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["url"],
            # For an http backing, `env` IS the attach-time header set.
            env=headers,
            pinned=ctx.pinned,
        )
    )


register(SPEC, build, validate=validate)

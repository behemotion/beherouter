"""mcp-stdio: any MCP server run as a subprocess, attached by command.

The generic counterpart of the curated `stdio` plugins (`plane`). Same rules as
`mcp-http`:

- `probe` and `pinned` are REQUIRED (`requires_entry`): no tested default.
- The credential is OPTIONAL. A subprocess takes it as an environment variable
  whose name only the server knows, so the operator names it in `api_key_env`;
  the two come as a pair.
- `cmd` must exist in the image. `_require_command` refuses a missing one at
  attach by name, and `registry-lint` warns about it offline.
- No `IdentitySupport`: a stdio subprocess's environment is fixed at spawn and
  reused across callers, so it can never be per-user.
"""

import re

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .spec import ConfigField, EnvVar, PluginContext, PluginSpec

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SPEC = PluginSpec(
    name="mcp-stdio",
    summary=(
        "Generic: any MCP server run as a subprocess, by command. Needs `probe` "
        "and `pinned` in the entry; the credential is optional."
    ),
    backing="stdio",
    requires_entry=("probe", "pinned"),
    config=(
        ConfigField("cmd", str, required=True,
                    doc="command line starting the server on stdio; must exist in the image"),
        ConfigField("api_key_env", str, default="",
                    doc="the subprocess environment variable that receives `api_key`"),
    ),
    env=(
        EnvVar(
            "api_key",
            doc="credential handed to the subprocess as `api_key_env`",
            required=False,
        ),
    ),
)


def validate(config: dict) -> None:
    """Offline. The `api_key`/`api_key_env` pairing is checked in build(),
    which is the first place that sees both halves."""
    if not (config.get("cmd") or "").strip():
        raise UsageError("mcp-stdio: cmd must not be empty")
    name = config.get("api_key_env") or ""
    if name and not _ENV_NAME.match(name):
        raise UsageError(
            f"mcp-stdio: api_key_env must be an environment variable name, got {name!r}"
        )


def subprocess_env(surface: str, config: dict, env: dict[str, str]) -> dict[str, str] | None:
    """The credential as the subprocess's environment, or None for none.

    Half a pair is refused rather than ignored: a key with nowhere to go is a
    surface that attaches, lists and 401s; a variable name with no key is a
    credential the operator believes is configured.
    """
    key, name = env.get("api_key"), config.get("api_key_env") or ""
    if key and not name:
        raise UsageError(
            f"'{surface}': [{surface}.env] api_key is set but [{surface}.config] "
            f"api_key_env is not; name the variable the server reads it from"
        )
    if name and not key:
        raise UsageError(
            f"'{surface}': [{surface}.config] api_key_env is set but "
            f"[{surface}.env] api_key is not"
        )
    return {name: key} if key else None


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="stdio",
            cmd=ctx.config["cmd"],
            env=subprocess_env(ctx.surface, ctx.config, ctx.env),
            pinned=ctx.pinned,
        )
    )


register(SPEC, build, validate=validate)

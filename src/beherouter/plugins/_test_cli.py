"""Any beheaxi CLI as a surface — the `cli` backing's only plugin-level caller.

Nothing live uses the `cli` backing, and the design says it ships with no
production plugin. But `CLIExecutor` and `load_cli_backend` are written and
tested, and without ONE caller that reaches them the way the gateway does, the
whole cli load path becomes unreachable from a registry entry and rots.

⚠️ REGISTERED ONLY WHEN BEHEROUTER_TEST_PLUGINS=1. `beherouter plugins` is a
pinned, agent-facing verb, and advertising a test fixture there would spend
every agent's context on something no deployment should attach.
"""

from ..backends.backing import CliBacking
from ..backends.cli import load_cli_backend
from . import register
from .spec import ConfigField, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="_test-cli",
    summary="Test-only: any beheaxi CLI as a surface. Not for production use.",
    backing="cli",
    config=(
        ConfigField(
            name="cmd",
            type=str,
            required=True,
            doc="console script implementing `describe --json`",
        ),
    ),
)


async def build(ctx: PluginContext):
    return load_cli_backend(
        CliBacking(name=ctx.surface, cmd=ctx.config["cmd"], pinned=ctx.pinned or None)
    )


register(SPEC, build)

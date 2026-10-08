"""Any beheaxi CLI as a surface, for tests — `beheaxi-cli` without its rules.

The production plugin is `beheaxi-cli`, which requires `probe`. This fixture
does not, so tests can reach the cli load path the way the gateway does without
restating a probe in every entry.

⚠️ REGISTERED ONLY WHEN BEHEROUTER_TEST_PLUGINS=1. `beherouter plugins` is a
pinned, agent-facing verb, and advertising a test fixture there would spend
every agent's context on something no deployment should attach.
"""

from ..backends.backing import CliBacking
from ..backends.cli import load_cli_backend
from . import register
from .spec import ConfigField, IdentitySupport, PluginContext, PluginSpec

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
    identity=IdentitySupport(modes=("claims",), target="env"),
)


async def build(ctx: PluginContext):
    return load_cli_backend(
        CliBacking(name=ctx.surface, cmd=ctx.config["cmd"], pinned=ctx.pinned or None)
    )


register(SPEC, build)

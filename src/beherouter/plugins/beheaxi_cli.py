"""beheaxi-cli: any beheaxi CLI as a surface, attached by command.

Foundation principle 1 — "beherouter reaches a tool by invoking its CLI" — was
reachable only from out-of-tree plugins until this one. The surface is derived
from `<cmd> describe --json`; each verb runs as `<cmd> <verb> … --json`, with
beheaxi's reserved exit codes mapped to gateway errors and domain codes (≥ 10)
returned as data (`backends/cli.py`, reused unchanged).

- `probe` is REQUIRED (`requires_entry`). A beheaxi manifest cannot declare a
  health verb, so there is no default to fall back to. Pick a read-only verb
  that touches whatever the CLI depends on. `pinned` is NOT required: the
  manifest's own per-verb `pinned` flag is the default, and an entry may
  override it by bare verb name.
- `cmd` must exist in the image; `registry-lint` warns when it is not on PATH.
- Identity: mode `claims` only, landing as subprocess environment (`target =
  "env"`), which `CLIExecutor` merges per call. The CLI must read it.
- No credential of its own. A CLI that needs one reads it from its own config
  today; add it here when a mounted CLI actually needs it.
"""

from ..backends.backing import CliBacking
from ..backends.cli import load_cli_backend
from ..errors import UsageError
from . import register
from .spec import ConfigField, IdentitySupport, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="beheaxi-cli",
    summary=(
        "Generic: any beheaxi CLI as a surface, from its `describe --json` "
        "manifest. Needs `probe` in the entry."
    ),
    backing="cli",
    requires_entry=("probe",),
    config=(
        ConfigField("cmd", str, required=True,
                    doc="console script implementing `describe --json`; must exist in the image"),
    ),
    identity=IdentitySupport(
        modes=("claims",),
        target="env",
        doc="asserted claims land as the verb subprocess's environment",
    ),
)


def validate(config: dict) -> None:
    if not (config.get("cmd") or "").strip():
        raise UsageError("beheaxi-cli: cmd must not be empty")


async def build(ctx: PluginContext):
    return load_cli_backend(
        CliBacking(name=ctx.surface, cmd=ctx.config["cmd"], pinned=ctx.pinned or None)
    )


register(SPEC, build, validate=validate)

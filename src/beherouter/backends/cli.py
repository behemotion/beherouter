"""cli backend: derive a surface from `<cmd> describe --json` and run its verbs."""

import asyncio
import json
import os
import shlex
import subprocess

from ..errors import (
    AuthError,
    AxiError,
    Conflict,
    ExitCode,
    NotFound,
    Unavailable,
    UsageError,
)
from ..manifest import validate_manifest
from ..models import Backend, ToolDescriptor
from ..naming import detect_collisions, flatten
from .backing import CliBacking


def _describe(cmd: str) -> dict:
    """Run `<cmd> describe --json` and parse the manifest."""
    try:
        proc = subprocess.run(
            shlex.split(cmd) + ["describe", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,  # non-zero is reported as Unavailable below, not raised
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        raise Unavailable(f"could not run '{cmd} describe --json': {e}") from e
    if proc.returncode != 0:
        raise Unavailable(
            f"'{cmd} describe --json' exited {proc.returncode}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise Unavailable(f"'{cmd} describe --json' did not emit JSON: {e}") from e


def build_argv(cmd: str, verb: str, schema: dict, args: dict) -> list[str]:
    """Render a manifest verb plus an args dict into an argument vector.

    beheaxi convention (describe.py:_arg_entry): REQUIRED args are positional
    (bare name), OPTIONAL args render as `--flag-name`. The manifest's
    `required` field is the source of truth here, NOT whether the wire name
    happens to carry leading dashes — a manifest that spells an optional arg
    without them still produces a flag.

    Positional order follows the manifest, which `load_cli_backend` preserves
    by building `schema` from `v["args"]` in order.
    """
    known = {}
    for wire, spec in schema.items():
        known[wire] = spec
        known[wire.lstrip("-")] = spec

    unknown = [k for k in args if k not in known]
    if unknown:
        raise UsageError(f"'{verb}': unknown argument(s) {', '.join(sorted(unknown))}")

    positionals: list[str] = []
    flags: list[str] = []
    for wire, spec in schema.items():
        bare = wire.lstrip("-")
        if wire in args:
            value = args[wire]
        elif bare in args:
            value = args[bare]
        else:
            value = None
        if value is None:
            if spec.get("required"):
                raise UsageError(f"'{verb}': missing required argument '{bare}'")
            continue
        if spec.get("required"):
            positionals.append(str(value))
        elif isinstance(value, bool):
            # A boolean flag is presence/absence, never `--flag False`.
            if value:
                flags.append(wire if wire.startswith("-") else f"--{bare}")
        else:
            flags.extend([wire if wire.startswith("-") else f"--{bare}", str(value)])
    return shlex.split(cmd) + [verb] + positionals + flags


# beheaxi reserves exit codes 0-9 (CONVENTIONS.md); >=10 is the tool's own.
_RESERVED: dict[int, type[AxiError]] = {
    ExitCode.INTERNAL: AxiError,
    ExitCode.USAGE: UsageError,
    ExitCode.NOT_FOUND: NotFound,
    ExitCode.AUTH: AuthError,
    ExitCode.CONFLICT: Conflict,
    ExitCode.UNAVAILABLE: Unavailable,
}

DOMAIN_EXIT_FLOOR = 10


def _exit_error(tool: str, verb: str, code: int, stderr: str) -> Exception:
    """Map a RESERVED exit code onto the matching error class.

    Only reached for codes < DOMAIN_EXIT_FLOOR; `run()` returns domain codes as
    data instead, because a domain failure is a result the agent should read,
    not a transport fault that should abort the call.
    """
    cls = _RESERVED.get(code, Unavailable)
    return cls(f"'{tool}': verb '{verb}' exited {code}: {stderr}")


class CLIExecutor:
    """Run a beheaxi CLI verb as a subprocess and return its JSON object.

    `asyncio.create_subprocess_exec` rather than `subprocess.run`: `run()` is on
    the request path and this process serves every surface, so a blocking call
    stalls all of them, not just this backend. (`_describe` above may stay
    blocking — it runs once, at attach.)

    The command is an argument VECTOR, never a shell string: `args` is
    agent-supplied and must not reach a shell.
    """

    def __init__(
        self, tool: str, cmd: str, schemas: dict[str, dict], timeout: float = 60.0
    ) -> None:
        self.tool, self.cmd, self.schemas, self.timeout = tool, cmd, schemas, timeout

    async def run(self, verb: str, args: dict, *, identity=None) -> dict:
        schema = self.schemas.get(verb)
        if schema is None:
            raise UsageError(f"'{self.tool}': unknown verb '{verb}'")
        argv = build_argv(self.cmd, verb, schema, args or {})
        # Ask for machine output. A beheaxi CLI prints a HUMAN dashboard by
        # default and JSON only under `--json` (CONVENTIONS.md, beheaxi CLI
        # profile), so without this every real cli call parses a Python-repr
        # dashboard and dies as Unavailable. Not folded into build_argv: that
        # renders the AGENT's arguments, while output format is the gateway's
        # own concern. Conditional so a manifest that declares its own --json
        # arg does not get it twice.
        if "--json" not in argv:
            argv.append("--json")
        # Identity arrives as environment, MERGED ON TOP of this process's own:
        # replacing it outright would take PATH with it, and a beheaxi CLI that
        # cannot find its helper binaries fails as Unavailable for a reason
        # nothing in the message explains. None keeps the plain inheritance.
        env = (
            {**os.environ, **identity.env}
            if identity is not None and identity.env
            else None
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            raise Unavailable(f"'{self.tool}': could not run {argv[0]!r}: {e}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise Unavailable(
                f"'{self.tool}': verb '{verb}' exceeded {self.timeout}s"
            ) from e
        if proc.returncode >= DOMAIN_EXIT_FLOOR:
            return {
                "exit_code": proc.returncode,
                "stderr": err.decode().strip(),
                "stdout": out.decode().strip(),
            }
        if proc.returncode != 0:
            raise _exit_error(self.tool, verb, proc.returncode, err.decode().strip())
        text = out.decode().strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise Unavailable(
                f"'{self.tool}': verb '{verb}' did not emit JSON: {e}"
            ) from e


def load_cli_backend(backing: CliBacking) -> Backend:
    """Build a Backend from a beheaxi CLI's manifest. Raises Unavailable/Conflict."""
    manifest = _describe(backing.cmd)
    validate_manifest(manifest)
    verbs = [v["name"] for v in manifest["verbs"]]
    detect_collisions(backing.name, verbs)
    override = set(backing.pinned) if backing.pinned else None
    descriptors = [
        ToolDescriptor(
            name=flatten(backing.name, v["name"]),
            verb=v["name"],
            summary=v["summary"],
            schema={a["name"]: a for a in v["args"]},
            pinned=(v["name"] in override) if override is not None else v["pinned"],
            mutating=v["mutating"],
        )
        for v in manifest["verbs"]
    ]
    schemas = {v["name"]: {a["name"]: a for a in v["args"]} for v in manifest["verbs"]}
    return Backend(
        name=backing.name,
        kind="cli",
        descriptors=descriptors,
        executor=CLIExecutor(backing.name, backing.cmd, schemas),
    )

"""python-dir: drop a file, get tools — a directory of `@tool` functions.

A source on the `inproc` seam: `build()` hands the directory to
`backends.inproc.python_dir_server`, which imports it and refuses every way
FastMCP's own directory provider fails open, then to `load_inproc_backend`.
This module imports NOTHING from FastMCP.

⚠️ THIS RUNS OPERATOR-SUPPLIED CODE INSIDE THE GATEWAY PROCESS — the same trust
as installing a plugin package. Only `build()` imports it. Lint (`validate`,
`warn`, `published`) reads the files with `ast` and never executes them, so a
workstation running `registry-lint` never runs the operator's code.

Rules (spec § Phase 1b — `python-dir` decisions):

- `path` is absolute. Absent on the linting machine → a warning, not a
  failure; lint runs where the directory usually is not.
- A syntax error fails lint, naming file and line.
- `probe` and `pinned` are required (`requires_entry`) and must name tools the
  static scan finds. The scan recognises top-level functions decorated `tool`
  (bare, called, or an attribute such as `fastmcp.tools.tool`); it is
  best-effort, and `python_dir_server`'s attach-time checks are authoritative.
- No identity support: a function that wants the caller has nowhere stable to
  read it from yet.
"""

import ast
from pathlib import Path

from ..backends.backing import McpBacking
from ..backends.inproc import load_inproc_backend, python_dir_server
from ..errors import UsageError
from . import register
from .spec import ConfigField, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="python-dir",
    summary=(
        "A directory of @tool-decorated Python functions, run in the gateway "
        "process (operator code: the trust of installing a plugin). Needs "
        "`path`, `probe` and `pinned`."
    ),
    backing="inproc",
    requires_entry=("probe", "pinned"),
    config=(
        ConfigField("path", str, required=True,
                    doc="absolute path of the directory of .py files, as the gateway sees it"),
    ),
)


def _files(root: Path) -> list[Path]:
    """The files discovery imports: recursive, no __init__.py, no __pycache__."""
    return sorted(
        p for p in root.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    )


def _str_const(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _tool_name(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        leaf = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if leaf != "tool":
            continue
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "name" and (name := _str_const(kw.value)) is not None:
                    return name
            if dec.args and (name := _str_const(dec.args[0])) is not None:
                return name
        return fn.name
    return None


def scan_tools(root: Path) -> dict[str, Path]:
    """Tool name -> the file declaring it, by parsing only. Raises on a syntax error."""
    found: dict[str, Path] = {}
    for file in _files(root):
        try:
            tree = ast.parse(file.read_text(), filename=str(file))
        except SyntaxError as e:
            raise UsageError(f"python-dir: {file}:{e.lineno}: {e.msg}") from e
        for node in tree.body:
            # Discovery skips module attributes starting '_'; so does this.
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
            ):
                name = _tool_name(node)
                if name is not None:
                    found.setdefault(name, file)
    return found


def validate(config: dict) -> None:
    """Offline, and never imports: a syntax error or a bad path is refused."""
    path = Path(config["path"])
    if not path.is_absolute():
        raise UsageError(f"python-dir: path must be absolute, got '{config['path']}'")
    if path.exists() and not path.is_dir():
        raise UsageError(f"python-dir: path '{path}' is not a directory")
    if path.is_dir():
        scan_tools(path)


def published(config: dict) -> set[str] | None:
    path = Path(config["path"])
    return set(scan_tools(path)) if path.is_dir() else None


def warn(config: dict) -> list[str]:
    path = Path(config["path"])
    if path.is_dir():
        return []
    return [
        (
            f"path '{path}' does not exist here; lint could not check the files' "
            f"syntax or that `pinned` and `probe` name tools in them"
        )
    ]


async def build(ctx: PluginContext):
    server = python_dir_server(ctx.config["path"], name=ctx.surface)
    return await load_inproc_backend(
        McpBacking(name=ctx.surface, transport="inproc", server=server, pinned=ctx.pinned)
    )


register(SPEC, build, validate=validate, warn=warn, published=published)

"""One argument path for pinned tools and run_tool.

Until 2026-09-25 only pinned tools normalised their arguments; `run_tool`
forwarded whatever it was given — so the `archive=True`-on-`create` failure
(see `prepare_args`) was fixed on the pinned path and still open on the long
tail. Both paths now call `prepare_args`.
"""

import keyword
import re
from typing import Any

from rapidfuzz import fuzz, process

from .errors import UsageError
from .models import ToolDescriptor
from .schema import is_json_schema

# beheaxi manifest arg types (manifest_schema.json) -> Python annotations.
PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

NO_DEFAULT = object()

_KEYWORDS = frozenset(keyword.kwlist)


def param_name(wire_name: str) -> str:
    """Turn a backend arg name into a valid Python parameter name.

    beheaxi renders OPTIONAL args as `--flag-name` (describe.py:`_arg_entry`), and
    upstream MCP servers can publish anything. `inspect.Parameter` requires a real
    identifier, so strip leading dashes and normalize separators. The original
    wire name is kept separately for forwarding.
    """
    name = wire_name.lstrip("-").replace("-", "_").replace(" ", "_")
    name = re.sub(r"\W", "_", name)
    if not name or name[0].isdigit():
        name = f"arg_{name}"
    if name in _KEYWORDS:
        name = f"{name}_"
    return name


def normalize_args(schema: dict) -> list[tuple[str, str, Any, bool, Any]]:
    """Return `(wire_name, param_name, python_type, required, default)`.

    Two dialects reach us (see `schema.is_json_schema`). Upstream MCP schemas
    carry `default` values; preserving them keeps the re-published schema
    faithful to the backend's own (`{"type": "integer", "default": 10}` rather
    than a lossy `anyOf[integer, null]`).
    """
    if not schema:
        return []
    if is_json_schema(schema):
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        raw = [
            (
                name,
                PY_TYPES.get(prop.get("type"), Any),
                name in required,
                prop.get("default", NO_DEFAULT),
            )
            for name, prop in properties.items()
            if isinstance(prop, dict)
        ]
    else:
        # beheaxi manifests have no `default` field (the schema is closed).
        raw = [
            (name, PY_TYPES.get(arg.get("type"), Any), bool(arg.get("required")), NO_DEFAULT)
            for name, arg in schema.items()
            if isinstance(arg, dict)
        ]

    out, seen = [], set()
    for wire, py_type, required, default in raw:
        param = param_name(wire)
        while param in seen:  # two wire names can sanitize to the same identifier
            param = f"{param}_"
        seen.add(param)
        out.append((wire, param, py_type, required, default))
    return out


def _is_closed(schema: dict) -> bool:
    """A closed schema declares every argument the backend accepts.

    JSON Schema says so with `additionalProperties: false`; a beheaxi manifest's
    arg set is complete by definition. An open schema is never second-guessed.
    """
    if is_json_schema(schema):
        return schema.get("additionalProperties") is False
    return True


def _enum(schema: dict, wire: str) -> list:
    spec = (schema.get("properties") or {}).get(wire) if is_json_schema(schema) else schema.get(wire)
    if not isinstance(spec, dict):
        return []
    for variant in (spec, *(spec.get("anyOf") or []), *(spec.get("oneOf") or [])):
        if isinstance(variant, dict) and variant.get("enum"):
            return list(variant["enum"])
    return []


def prepare_args(d: ToolDescriptor, args: dict) -> dict:
    """Map, drop and check `args` for `d`; return what the backend receives.

    1. Accept the wire name (`--flag-name`, what describe_tool shows) or the
       param spelling (`flag_name`, what a pinned tool publishes); forward the
       wire name. Both spellings of one arg is a UsageError.
    2. Drop `None` and any value equal to the schema's own default. The backend
       applies that default for an absent arg, so this is semantically
       identical — and it is not cosmetic: against an ACTION-PARAMETERIZED
       tool, whose schema is the UNION of every action's parameters, a default
       belonging to another action is a hard error. Plane's `workitem` declares
       `archive` (default True) for its archive action, so `action="create"`
       arrived carrying `archive=True` and was refused.
    3. Refuse a missing required arg (listing its enum) and, on a closed schema
       only, an undeclared one (suggesting the closest name).
    """
    specs = normalize_args(d.schema)
    to_wire = {}
    for wire, param, _t, _r, _dflt in specs:
        to_wire[wire] = wire
        to_wire.setdefault(param, wire)
    defaults = {w: dflt for w, _p, _t, req, dflt in specs if not req and dflt is not NO_DEFAULT}
    closed = _is_closed(d.schema)

    out: dict = {}
    source: dict[str, str] = {}
    for key, value in args.items():
        wire = to_wire.get(key)
        if wire is None:
            if closed:
                close = process.extractOne(key, list(to_wire), scorer=fuzz.ratio, score_cutoff=60)
                hint = f"; did you mean '{to_wire[close[0]]}'?" if close else ""
                raise UsageError(f"{d.name}: unknown arg '{key}'{hint}")
            wire = key
        if wire in source:
            raise UsageError(
                f"{d.name}: arg '{wire}' given twice, as '{source[wire]}' and "
                f"'{key}'; pass one spelling, not both"
            )
        source[wire] = key
        if value is None or (wire in defaults and value == defaults[wire]):
            continue
        out[wire] = value

    for wire, _p, _t, required, _dflt in specs:
        if required and wire not in out:
            choices = _enum(d.schema, wire)
            tail = f" (one of: {', '.join(map(str, choices))})" if choices else ""
            raise UsageError(f"{d.name}: missing required arg '{wire}'{tail}")
    return out

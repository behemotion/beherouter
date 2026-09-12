"""Flatten beheaxi verb names to flat MCP tool names; detect collisions."""

from .errors import Conflict


def flatten(tool: str, verb: str) -> str:
    """`behelib` + `shelf-create` (or `shelf create`) -> `behelib_shelf_create`.

    beheaxi v0.1.0 emits flat, hyphenated verb names (fn `__name__` with `_`->`-`);
    there are no nested command groups yet. Normalize BOTH spaces and hyphens to
    underscores so the rule is correct today and forward-compatible with any
    future space-joined nested-group form.
    """
    norm = verb.replace("-", " ").split()
    return "_".join([tool, *norm])


def detect_collisions(tool: str, verbs: list[str]) -> None:
    """Raise Conflict if two *distinct* verbs map to the same flat MCP tool name."""
    seen: dict[str, str] = {}
    for verb in verbs:
        name = flatten(tool, verb)
        if name in seen and seen[name] != verb:
            raise Conflict(
                f"verb-name collision in '{tool}': '{seen[name]}' and '{verb}' "
                f"both map to MCP tool '{name}'"
            )
        seen[name] = verb

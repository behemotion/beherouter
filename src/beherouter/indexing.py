"""The single place descriptors become search-index entries.

Both the MCP surface (`surface.py`) and the `beherouter search` verb
(`cli/app.py`) index the same descriptors. They built the index independently
until 2026-08-04, which meant enriching one silently diverged the two — the same
query returning different results depending on how it was asked.
"""

from .models import ToolDescriptor
from .search import ToolIndex


def _arg_tokens(schema: dict) -> list[str]:
    """Arg names and enum values, as extra search keywords.

    CEILING: beheaxi manifests carry no per-arg DESCRIPTION field
    (describe.py:_arg_entry emits name/type/required/enum only), so for cli
    backends this is as rich as the index can get. Going further means changing
    the manifest schema, which is an umbrella-level decision — see
    BEHEMOTION/docs/CONVENTIONS.md, "changes to a rule happen here first".
    """
    tokens: list[str] = []
    for wire, spec in (schema or {}).items():
        tokens.append(wire.lstrip("-").replace("-", " "))
        if isinstance(spec, dict):
            tokens.extend(str(v) for v in spec.get("enum") or [])
    return tokens


def build_index(descriptors: list[ToolDescriptor]) -> ToolIndex:
    index = ToolIndex()
    for d in descriptors:
        index.add(d.name, d.summary, [d.verb, *_arg_tokens(d.schema)])
    return index

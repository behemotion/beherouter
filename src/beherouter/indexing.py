"""The single place descriptors become search-index entries — and search hits.

Both the MCP surface (`surface.py`) and the `beherouter search` verb
(`cli/app.py`) index the same descriptors and emit the same hits. They built
both independently until 2026-08-04, which meant enriching one silently
diverged the two — the same query returning different results depending on how
it was asked.
"""

from collections.abc import Mapping

from .models import ToolDescriptor
from .schema import is_json_schema
from .search import ToolIndex, brief


def _arg_tokens(schema: dict) -> list[str]:
    """Arg names, enum values and (JSON Schema only) arg descriptions.

    Dialect-aware: iterating a JSON Schema's top level would index its own
    keywords (`properties`, `required`, ...) into every MCP tool and never the
    arguments — which it did until 2026-09-25.

    CEILING for cli backends: beheaxi manifests carry no per-arg DESCRIPTION
    (describe.py:_arg_entry emits name/type/required/enum only). Going further
    means changing the manifest schema — an umbrella-level decision, see
    BEHEMOTION/docs/CONVENTIONS.md.
    """
    tokens: list[str] = []
    if is_json_schema(schema):
        for name, prop in (schema.get("properties") or {}).items():
            tokens.append(name)
            if not isinstance(prop, dict):
                continue
            if isinstance(prop.get("description"), str):
                tokens.append(prop["description"])
            for variant in (prop, *(prop.get("anyOf") or []), *(prop.get("oneOf") or [])):
                if isinstance(variant, dict):
                    tokens.extend(str(v) for v in variant.get("enum") or [])
        return tokens
    for wire, spec in (schema or {}).items():
        tokens.append(wire.lstrip("-").replace("-", " "))
        if isinstance(spec, dict):
            tokens.extend(str(v) for v in spec.get("enum") or [])
    return tokens


def build_index(
    descriptors: list[ToolDescriptor],
    aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> ToolIndex:
    aliases = aliases or {}
    index = ToolIndex()
    for d in descriptors:
        index.add(
            d.name, d.summary, [d.verb, *_arg_tokens(d.schema)], aliases.get(d.name, ())
        )
    return index


def search_hits(
    index: ToolIndex,
    by_name: dict[str, ToolDescriptor],
    query: str,
    limit: int,
    published: set[str],
) -> list[dict]:
    """The hit shape, for search_tools and `beherouter search` alike.

    A one-line `brief`, not the description: on Plane a full-description page
    of 10 hits cost ~4 300 tokens, more than pinning saved. `mutating` is sent
    only when the backend said; `pinned` only when true (call it directly).
    """
    hits = []
    for name in index.search(query, limit=limit):
        d = by_name[name]
        hit: dict = {"name": d.name, "brief": brief(d.summary)}
        if d.mutating is not None:
            hit["mutating"] = d.mutating
        if d.name in published:
            hit["pinned"] = True
        hits.append(hit)
    return hits

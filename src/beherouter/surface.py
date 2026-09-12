"""Turn a Backend into a FastMCP surface: pinned flat tools + search/describe/run.

The context-control mechanism (AGENTS.md): each surface advertises only a few
pinned flat tools plus three meta-tools, so a ~100-tool backend costs a handful of
tool definitions in the client's context instead of a hundred. The long tail is
reached through `search_tools` -> `describe_tool` -> `run_tool`.
"""

import inspect
import keyword
import re
from typing import Any

from fastmcp import FastMCP

from .catalogue import Catalogue
from .models import Backend, ToolDescriptor

# beheaxi manifest arg types (manifest_schema.json) -> Python annotations.
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


_NO_DEFAULT = object()

_KEYWORDS = frozenset(keyword.kwlist)


def _param_name(wire_name: str) -> str:
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


def _normalize_args(schema: dict) -> list[tuple[str, str, Any, bool, Any]]:
    """Return `(wire_name, param_name, python_type, required, default)`.

    Two dialects reach us:
      * `cli` backends  -> `{arg_name: {"name","type","required"}}` (beheaxi manifest)
      * `mcp` backends  -> a JSON Schema object with `properties` / `required`

    Upstream MCP schemas carry `default` values; preserving them keeps the
    re-published schema faithful to the backend's own (`{"type": "integer",
    "default": 10}` rather than a lossy `anyOf[integer, null]`).
    """
    if not schema:
        return []
    # A zero-argument MCP tool publishes a bare `{"type": "object"}` with no
    # `properties` at all (4 of gitea-mcp's 50 tools do). That is still JSON
    # Schema, so detect the dialect by its marker keys rather than by whether
    # `properties` happens to be present.
    is_json_schema = (
        "properties" in schema or "$schema" in schema or schema.get("type") == "object"
    )
    if is_json_schema:
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        raw = [
            (
                name,
                _PY_TYPES.get(prop.get("type"), Any),
                name in required,
                prop.get("default", _NO_DEFAULT),
            )
            for name, prop in properties.items()
            if isinstance(prop, dict)
        ]
    else:
        # beheaxi manifests have no `default` field (the schema is closed).
        raw = [
            (name, _PY_TYPES.get(arg.get("type"), Any), bool(arg.get("required")), _NO_DEFAULT)
            for name, arg in schema.items()
            if isinstance(arg, dict)
        ]

    out, seen = [], set()
    for wire, py_type, required, default in raw:
        param = _param_name(wire)
        while param in seen:  # two wire names can sanitize to the same identifier
            param = f"{param}_"
        seen.add(param)
        out.append((wire, param, py_type, required, default))
    return out


def _make_pinned_tool(descriptor: ToolDescriptor, backend: Backend):
    """Build a callable with a REAL signature derived from the descriptor.

    FastMCP 3.x rejects `**kwargs` functions as tools, because it derives each
    tool's MCP `inputSchema` by introspecting the signature — a `**kwargs`
    function would advertise nothing, leaving the agent no idea what to pass.
    So we synthesize the signature instead. See docs/FASTMCP-NOTES.md.
    """

    normalized = _normalize_args(descriptor.schema)
    to_wire = {param: wire for wire, param, _, _, _ in normalized}
    # Every optional that carries an upstream default, by PARAM name. Needed
    # because the synthesized signature below uses that default as the Python
    # default (for schema fidelity), which means Python fills it in before this
    # function is ever entered — an omitted argument and an explicitly-passed
    # one are indistinguishable in kwargs.
    schema_defaults = {
        param: default
        for _wire, param, _py, required, default in normalized
        if not required and default is not _NO_DEFAULT
    }

    async def _tool(**kwargs):
        # Drop unset optionals, and forward under the backend's own arg names.
        #
        # "Unset" has to be inferred: a value equal to the schema's own default
        # is treated as omitted. That is safe in both directions — the backend
        # applies exactly that default for an absent argument, so omitting it is
        # semantically identical — and it is what stops Python's signature
        # defaults from being forwarded on every single call.
        #
        # ⚠️ This is not cosmetic. Against an ACTION-PARAMETERIZED tool, whose
        # schema is the UNION of every action's parameters, forwarding a default
        # belonging to a DIFFERENT action is a hard backend error. Plane's
        # `workitem` declares `archive` (default True) for its archive action, so
        # `workitem(action="create", ...)` used to arrive carrying `archive=True`
        # and was refused with "action 'create' does not take: archive". Reads
        # survived, so the surface looked healthy while every write failed.
        args = {
            to_wire.get(k, k): v
            for k, v in kwargs.items()
            if v is not None and not (k in schema_defaults and v == schema_defaults[k])
        }
        return await backend.executor.run(descriptor.verb, args)

    params, annotations = [], {}
    for _wire, param_name, py_type, required, default in normalized:
        if required:
            annotation, param_default = py_type, inspect.Parameter.empty
        elif default is not _NO_DEFAULT:
            # Optional WITH an upstream default: keep the plain type so the
            # republished schema matches the backend's ({"type": "integer",
            # "default": 10}).
            annotation, param_default = py_type, default
        else:
            # Optional with no default: `T | None` is the honest annotation.
            annotation = py_type | None if py_type is not Any else Any
            param_default = None
        annotations[param_name] = annotation
        params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=param_default,
            )
        )

    _tool.__signature__ = inspect.Signature(params)
    _tool.__annotations__ = annotations
    _tool.__name__ = descriptor.name
    _tool.__doc__ = descriptor.summary
    return _tool


# The MCP annotation hints we forward. `title` is deliberately excluded: it is a
# display name, not a safety signal, and beherouter's own tool name is the one
# the client already sees.
_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def republished_annotations(d: ToolDescriptor) -> dict | None:
    """The annotations beherouter publishes for a descriptor, or None.

    Emitted ONLY when the backend actually said something. A gateway that
    invents `readOnlyHint: true` for an unannotated tool is worse than one that
    stays silent: a host would then TRUST the annotation. Silence leaves the
    host's own confirmation policy in charge, which is the correct default.
    """
    if not d.annotations:
        return None
    out = {k: d.annotations[k] for k in _HINTS if k in d.annotations}
    return out or None


def wrapped_output_schema(d: ToolDescriptor) -> dict | None:
    """The backend's output schema, wrapped to match the {"result": ...} envelope.

    MCP executors (MCPClientExecutor, ReconnectingMCPExecutor in backends/mcp.py)
    return `{"result": <payload>}`, so republishing the backend's schema
    verbatim would declare a shape the tool never returns — and FastMCP
    validates output against the declaration, so it would fail on the first call
    rather than merely mislead.

    Non-MCP backings (CLI, calendar) do not populate `output_schema`, so this
    helper returns None for them; MCP backends are the only case where wrapping
    matters.

    Wrapping keeps the wire format unchanged for all five wired consumers while
    still giving a code-mode host precise types instead of `any`.
    """
    if not d.output_schema:
        return None
    return {
        "type": "object",
        "properties": {"result": d.output_schema},
        "required": ["result"],
    }


def register_pinned(mcp: FastMCP, d: ToolDescriptor, backend: Backend) -> None:
    """Publish one descriptor as a flat tool on `mcp`.

    The SINGLE place a descriptor becomes a published tool. `costing.py` builds
    a throwaway all-pinned surface through this same function, so the "what a
    direct connection would cost" figure is measured against the shape
    beherouter actually serves rather than a reimplementation of it.
    """
    mcp.tool(
        name=d.name,
        description=d.summary,
        annotations=republished_annotations(d),
        output_schema=wrapped_output_schema(d),
    )(_make_pinned_tool(d, backend))


def build_surface(backend: Backend, auth: Any | None = None) -> FastMCP:
    """Build the MCP surface for one attached backend.

    `auth` is a FastMCP auth provider (see `auth.SharedTokenVerifier`); when None
    the surface is unauthenticated, which is only appropriate in tests and for
    in-process use.
    """
    mcp = FastMCP(backend.name, auth=auth)

    # ⚠️ `search_tools`, `describe_tool`, `run_tool` and `context_cost` are
    # RESERVED published names on every surface. A backend that serves and pins
    # a tool of one of those names loses it: the meta-tool is registered after
    # the pinned set, and FastMCP REPLACES a duplicate component rather than
    # raising (it logs `Component already exists`; verified against 3.4.5). The
    # backend's tool is then unreachable through the published array -- though
    # still callable through `run_tool` -- and its definition is counted as
    # `tokens_meta` by `costing.META_TOOL_NAMES`, which matches by published
    # name. No backend has collided yet; `context_cost` newly joined the
    # reserved set in 2026-09.

    # ONE catalogue behind all four meta-tools. The pinned tools registered
    # below are NOT re-registered when it refreshes: the searchable half may
    # churn, the published `tools` array may not.
    catalogue = Catalogue(
        backend.descriptors,
        relist=backend.relist,
        ttl_ms=backend.ttl_ms,
        name=backend.name,
    )

    # The FROZEN published set, captured once from attach-time descriptors --
    # never from the catalogue, which can refresh. A descriptor's own `.pinned`
    # is recomputed by the backend against the CONFIGURED pin list on every
    # re-list (backends/mcp.py), so a tool named in registry.toml's pin list
    # but absent at attach (a Community-Edition 404, say) would come back
    # `pinned: true` after a later refresh even though registration is frozen
    # and it was never added to the published `tools` array. Reporting
    # membership in THIS set instead is what keeps a meta-tool from telling an
    # agent something the array contradicts.
    published_names = {d.name for d in backend.pinned}

    for d in backend.pinned:
        register_pinned(mcp, d, backend)

    @mcp.tool(
        description=(
            "Search this surface's tools. Returns each match with its description, "
            "so no follow-up describe_tool call is needed to know what a tool does."
        )
    )
    async def search_tools(query: str, limit: int = 10) -> list[dict]:
        await catalogue.ensure_fresh()
        by_name = catalogue.by_name
        hits = []
        for name in catalogue.index.search(query, limit=limit):
            d = by_name[name]
            hits.append(
                {
                    "name": d.name,
                    "summary": d.summary,
                    "pinned": d.name in published_names,
                    "mutating": d.mutating,
                    "annotations": d.annotations,
                }
            )
        return hits

    @mcp.tool(description="Return the argument schema + summary for a tool name.")
    async def describe_tool(name: str) -> dict:
        await catalogue.ensure_fresh()
        d = catalogue.by_name.get(name)
        if d is None:
            return {"error": f"unknown tool '{name}'"}
        return {
            "name": d.name,
            "summary": d.summary,
            "mutating": d.mutating,
            "pinned": d.name in published_names,
            "callable": True,
            "args": d.schema,
            "annotations": d.annotations,
            "returns": wrapped_output_schema(d),
        }

    @mcp.tool(description="Invoke any tool on this surface by name with an args object.")
    async def run_tool(name: str, args: dict | None = None) -> dict:
        await catalogue.ensure_fresh()
        d = catalogue.by_name.get(name)
        if d is None:
            return {"error": f"unknown tool '{name}'"}
        return await backend.executor.run(d.verb, args or {})

    @mcp.tool(
        description=(
            "Report what this surface costs a client's context: tokens for the "
            "published tools, and what publishing the whole catalogue would "
            "cost. Pass context_window to get both as a percentage of it."
        ),
        annotations={"readOnlyHint": True},
    )
    async def context_cost(context_window: int | None = None) -> dict:
        # Computed lazily rather than at build time: build_surface is sync and
        # FastMCP.list_tools() is not, and a lazily-computed figure stays
        # correct once the searchable catalogue starts refreshing (Phase 3).
        from .costing import as_payload, surface_cost
        from .errors import AxiError, Unavailable

        await catalogue.ensure_fresh()
        # CLASSIFIED, not swallowed. CONVENTIONS asks that every error crossing
        # the gateway boundary be an AxiError; a measurement failure here is an
        # `Unavailable`. Returning a green payload instead would be worse than
        # the bare exception -- a failing tool call must keep surfacing its
        # error. An error that is ALREADY an AxiError (a bad `context_window`
        # is a UsageError) passes through unrelabelled.
        try:
            cost = await surface_cost(mcp, backend, descriptors=catalogue.descriptors)
        except AxiError:
            raise
        except Exception as e:
            raise Unavailable(
                f"could not measure surface '{backend.name}': {e}"
            ) from e
        payload = as_payload(cost, context_window)
        # The live Catalogue's freshness rides here rather than on the health
        # record: health.check_entry attaches fresh in a separate process with
        # no baseline, so it cannot observe drift or staleness at all.
        payload["catalogue"] = catalogue.status
        if catalogue.drift.changed:
            payload["catalogue_drift"] = {
                "added": list(catalogue.drift.added),
                "removed": list(catalogue.drift.removed),
                "pinned_missing": list(catalogue.drift.pinned_missing),
            }
        return payload

    return mcp

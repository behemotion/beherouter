"""Turn a Backend into a FastMCP surface: pinned flat tools + search/describe/run.

The context-control mechanism (AGENTS.md): each surface advertises only a few
pinned flat tools plus three meta-tools, so a ~100-tool backend costs a handful of
tool definitions in the client's context instead of a hundred. The long tail is
reached through `search_tools` -> `describe_tool` -> `run_tool`.
"""

import inspect
import logging
from typing import Any

from fastmcp import FastMCP

from .args import NO_DEFAULT, normalize_args, prepare_args
from .catalogue import Catalogue
from .errors import NotFound
from .indexing import search_hits
from .models import Backend, ToolDescriptor
from .search import DEFAULT_LIMIT, suggest

logger = logging.getLogger(__name__)


def _make_pinned_tool(descriptor: ToolDescriptor, backend: Backend, dispatch):
    """Build a callable with a REAL signature derived from the descriptor.

    FastMCP 3.x rejects `**kwargs` functions as tools, because it derives each
    tool's MCP `inputSchema` by introspecting the signature — a `**kwargs`
    function would advertise nothing, leaving the agent no idea what to pass.
    So we synthesize the signature instead. See docs/FASTMCP-NOTES.md.
    """

    normalized = normalize_args(descriptor.schema)

    async def _tool(**kwargs):
        # One argument path for pinned tools and run_tool: see args.prepare_args.
        return await dispatch(descriptor.verb, prepare_args(descriptor, kwargs))

    params, annotations = [], {}
    for _wire, param_name, py_type, required, default in normalized:
        if required:
            annotation, param_default = py_type, inspect.Parameter.empty
        elif default is not NO_DEFAULT:
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


def register_pinned(
    mcp: FastMCP, d: ToolDescriptor, backend: Backend, dispatch=None
) -> None:
    """Publish one descriptor as a flat tool on `mcp`.

    The SINGLE place a descriptor becomes a published tool. `costing.py` builds
    a throwaway all-pinned surface through this same function, so the "what a
    direct connection would cost" figure is measured against the shape
    beherouter actually serves rather than a reimplementation of it.

    `dispatch` is the identity-aware call path built by `build_surface`. It
    defaults to the backend's own executor so `costing.py`'s throwaway
    all-pinned surface — which measures definitions and never calls anything —
    keeps working unchanged.
    """
    runner = dispatch or (lambda verb, args: backend.executor.run(verb, args))
    mcp.tool(
        name=d.name,
        description=d.summary,
        annotations=republished_annotations(d),
        output_schema=wrapped_output_schema(d),
    )(_make_pinned_tool(d, backend, runner))


def build_surface(
    backend: Backend, auth: Any | None = None, policy: Any | None = None
) -> FastMCP:
    """Build the MCP surface for one attached backend.

    `auth` is a FastMCP auth provider (see `auth.build_verifier`); when None
    the surface is unauthenticated, which is only appropriate in tests and for
    in-process use.

    `policy` is this surface's `identity.IdentityPolicy`. When it is None or
    disabled every dispatch passes `identity=None` and the call path is
    byte-identical to a gateway with no identity configuration at all — which is
    what makes this change invisible to the four static-token consumers.
    """
    mcp = FastMCP(backend.name, auth=auth)
    enabled = policy is not None and policy.enabled

    async def dispatch(verb: str, args: dict) -> dict:
        """The ONE call path. Identity is resolved here, per call, and nowhere
        else: `policy.resolve()` reads FastMCP's request context, which exists
        only here — not in `health.check_entry`, not in the CLI.
        """
        identity = policy.resolve() if enabled else None
        if identity is not None:
            # Names only. A value here would put a credential in the log.
            logger.info(
                "identity applied surface=%s verb=%s subject=%s mode=%s keys=%s",
                backend.name,
                verb,
                identity.subject,
                policy.mode,
                sorted({**identity.headers, **identity.env, **identity.credentials}),
            )
        return await backend.executor.run(verb, args, identity=identity)

    def guard() -> None:
        """`dispatch`'s refusal half, for the READ-ONLY meta-tools.

        A surface that refuses a caller's calls must also refuse to enumerate
        itself to them — "you do not have access to this surface" is a thin
        answer if `search_tools` still lists every tool on it. Called BEFORE
        `catalogue.ensure_fresh()` so a refused caller cannot drive a re-list
        of a backend they may not use.

        It gates without materialising: the catalogue is read with the
        deployment credential by design, so a `lookup` surface with an
        unreadable map must still answer a search for a caller who holds the
        role. ⚠️ The FROZEN published `tools` array is captured at attach and
        is NOT gated — a host sees it at connect time, before any of this runs.
        """
        if enabled:
            policy.guard()

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
        aliases=backend.search_aliases,
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

    def unknown_tool(name: str) -> NotFound:
        close = suggest(name, list(catalogue.by_name), catalogue.index)
        hint = (
            f" Did you mean: {', '.join(close)}?"
            if close
            else " Use search_tools to find one."
        )
        return NotFound(f"unknown tool '{name}' on surface '{backend.name}'.{hint}")

    for d in backend.pinned:
        register_pinned(mcp, d, backend, dispatch)

    @mcp.tool(
        description=(
            "Search this surface's tools. Returns name + one-line brief; call "
            "describe_tool for arguments before run_tool."
        )
    )
    async def search_tools(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        guard()
        await catalogue.ensure_fresh()
        return search_hits(
            catalogue.index, catalogue.by_name, query, limit, published_names
        )

    @mcp.tool(description="Return the argument schema + summary for a tool name.")
    async def describe_tool(name: str) -> dict:
        guard()
        await catalogue.ensure_fresh()
        d = catalogue.by_name.get(name)
        if d is None:
            raise unknown_tool(name)
        out = {
            "name": d.name,
            "summary": d.summary,
            "mutating": d.mutating,
            "pinned": d.name in published_names,
            "args": d.schema,
        }
        if d.annotations:
            out["annotations"] = d.annotations
        returns = wrapped_output_schema(d)
        if returns:
            out["returns"] = returns
        return out

    @mcp.tool(description="Invoke any tool on this surface by name with an args object.")
    async def run_tool(name: str, args: dict | None = None) -> dict:
        guard()
        await catalogue.ensure_fresh()
        d = catalogue.by_name.get(name)
        if d is None:
            raise unknown_tool(name)
        return await dispatch(d.verb, prepare_args(d, args or {}))

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

        guard()
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

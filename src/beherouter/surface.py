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
from fastmcp.exceptions import NotFoundError, ValidationError
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError as PydanticValidationError

from .args import NO_DEFAULT, normalize_args, prepare_args
from .audit import AuditSink, audit_claims
from .catalogue import Catalogue
from .costing import META_TOOL_NAMES
from .errors import AuthError, AxiError, NotFound, UsageError, tag
from .indexing import search_hits
from .metrics import UNKNOWN_TOOL
from .models import Backend, ToolDescriptor
from .pipeline import CallPipeline
from .search import DEFAULT_LIMIT, suggest

logger = logging.getLogger(__name__)

# FastMCP 3.4.5 (server/server.py, `call_tool`) logs a schema-validation
# failure as WARNING with pydantic's errors() -- `input` values included, i.e.
# the caller's arguments. The surface records that call itself
# (_RecordRefusedCall); this keeps the arguments out of FastMCP's line.
_INVALID_ARGUMENTS_MSG = "Invalid arguments for tool %r: %s"


class _RedactInvalidArguments(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.msg == _INVALID_ARGUMENTS_MSG
            and isinstance(record.args, tuple)
            and len(record.args) == 2
        ):
            record.args = (record.args[0], "<redacted>")
        return True


def _install_redaction() -> None:
    # On the emitting logger, so it holds for every handler (a test's, a
    # deployment's) whether or not logsetup.configure() ran. Idempotent.
    fastmcp_server = logging.getLogger("fastmcp.server.server")
    if not any(isinstance(f, _RedactInvalidArguments) for f in fastmcp_server.filters):
        fastmcp_server.addFilter(_RedactInvalidArguments())


_install_redaction()


def _make_pinned_tool(descriptor: ToolDescriptor, backend: Backend, runner):
    """Build a callable with a REAL signature derived from the descriptor.

    FastMCP 3.x rejects `**kwargs` functions as tools, because it derives each
    tool's MCP `inputSchema` by introspecting the signature — a `**kwargs`
    function would advertise nothing, leaving the agent no idea what to pass.
    So we synthesize the signature instead. See docs/FASTMCP-NOTES.md.
    """

    normalized = normalize_args(descriptor.schema)

    async def _tool(**kwargs):
        # One argument path for pinned tools and run_tool: the runner calls
        # args.prepare_args, so its errors are classified in the PREPARE phase.
        return await runner(descriptor, kwargs)

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

    _tool.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
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
    mcp: FastMCP, d: ToolDescriptor, backend: Backend, runner=None
) -> None:
    """Publish one descriptor as a flat tool on `mcp`.

    The SINGLE place a descriptor becomes a published tool. `costing.py` builds
    a throwaway all-pinned surface through this same function, so the "what a
    direct connection would cost" figure is measured against the shape
    beherouter actually serves rather than a reimplementation of it.

    `runner(d, kwargs)` is the call path built by `build_surface` (through its
    `CallPipeline`). It defaults to the backend's own executor so `costing.py`'s
    throwaway all-pinned surface — which measures definitions and never calls
    anything — keeps working unchanged.
    """

    async def _direct(desc: ToolDescriptor, kwargs: dict):
        return await backend.executor.run(desc.verb, prepare_args(desc, kwargs))

    runner = runner or _direct
    mcp.tool(
        name=d.name,
        description=d.summary,
        annotations=republished_annotations(d),
        output_schema=wrapped_output_schema(d),
    )(_make_pinned_tool(d, backend, runner))


class _GateListing(Middleware):
    """Hide a gated surface's tools from a caller its gate refuses.

    `tools/call` is still the real gate; this keeps a host from putting tools
    in front of a model that can only ever be refused. Three rules:

    - FILTER, NEVER RAISE: an error in `tools/list` makes most hosts mark the
      whole server failed, so any refusal — a misconfiguration included —
      lists nothing (fail closed) instead of propagating.
    - GATE, NEVER MATERIALISE: `policy.guard()`, exactly as the meta-tools do,
      so a `lookup` surface with an unreadable map still lists for a holder.
    - Hosts cache the list: a role granted mid-session appears on reconnect.

    ⚠️ `FastMCP.list_tools()` runs middleware by default, so an in-process
    measurement with no request in hand would see `[]`. `costing.surface_cost`
    lists with `run_middleware=False` for that reason.
    """

    def __init__(self, policy: Any) -> None:
        self.policy = policy

    async def on_list_tools(self, context, call_next):
        try:
            self.policy.guard()
        except AuthError:
            return []
        except AxiError as e:
            # Not a caller's fault: a gate that cannot be evaluated. Logged,
            # because unlike a role miss it is not routine.
            logger.warning(
                "tools/list hidden on surface=%s: gate misconfigured: %s",
                self.policy.surface,
                e,
            )
            return []
        return await call_next(context)


def _invalid_arguments(tool: str, exc: ValidationError) -> UsageError:
    """Name each failing field and why -- never the value (pydantic's own
    message and errors() both carry `input`)."""
    cause = exc.__cause__
    if isinstance(cause, PydanticValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(arguments)'}: {err['msg']}"
            for err in cause.errors(include_input=False, include_url=False)
        )
    else:
        problems = "arguments do not match the tool's schema"
    return UsageError(f"invalid arguments for tool '{tool}': {problems}")


class _RecordRefusedCall(Middleware):
    """Route the calls FastMCP refuses BEFORE a tool function runs through the
    call pipeline, so they are audited, counted and carry `_meta` like any other.

    Two such refusals exist in 3.4.5: an unpublished name (`NotFoundError`,
    from the tool lookup) and arguments that fail the published schema
    (`ValidationError`, from FunctionTool's argument validation). Neither can
    come from inside a tool: every tool here returns its errors through
    `CallPipeline.run` instead of raising, so a call is never recorded twice.
    The label is the requested name only when it is a published tool name;
    otherwise caller input would become a metric label.
    """

    def __init__(self, pipeline: CallPipeline, published: set[str], unknown_tool) -> None:
        self.pipeline = pipeline
        self.published = published
        self.unknown_tool = unknown_tool

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        try:
            return await call_next(context)
        except ValidationError as e:
            refusal: AxiError = tag(_invalid_arguments(name, e), "bad_arguments")
        except NotFoundError:
            refusal = self.unknown_tool(name)
        label = name if name in self.published else UNKNOWN_TOOL

        async def work(scope):
            raise refusal

        return await self.pipeline.run(label, work)


def build_surface(
    backend: Backend,
    auth: Any | None = None,
    policy: Any | None = None,
    *,
    call_timeout_s: float | None = None,
    audit: AuditSink | None = None,
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
    # None unless identity is configured AND enabled; one name, so the closures
    # below narrow on it instead of re-deriving it from a bool.
    active = policy if policy is not None and policy.enabled else None
    if active is not None and active.hides_listing:
        mcp.add_middleware(_GateListing(active))

    # Every published tool -- pinned and meta alike -- goes through this one
    # pipeline. It gates WITHOUT materialising (`policy.guard()`, before any
    # catalogue re-list, so a refused caller cannot drive one), resolves the
    # caller's identity per call, and classifies/records the outcome. See
    # pipeline.CallPipeline.
    pipeline = CallPipeline(
        backend.name,
        backend.executor,
        policy,
        call_timeout_s=call_timeout_s,
        audit=audit,
        audit_claim_names=audit_claims(),
    )

    async def run_pinned(d: ToolDescriptor, kwargs: dict):
        async def work(scope):
            return await scope.execute(d.verb, prepare_args(d, kwargs))

        return await pipeline.run(d.name, work)

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

    def unknown_tool(name: str) -> AxiError:
        close = suggest(name, list(catalogue.by_name), catalogue.index)
        hint = (
            f" Did you mean: {', '.join(close)}?"
            if close
            else " Use search_tools to find one."
        )
        return tag(
            NotFound(f"unknown tool '{name}' on surface '{backend.name}'.{hint}"),
            "unknown_tool",
            suggestions=list(close),
        )

    for d in backend.pinned:
        register_pinned(mcp, d, backend, run_pinned)
    mcp.add_middleware(
        _RecordRefusedCall(
            pipeline,
            published_names | META_TOOL_NAMES,
            unknown_tool,
        )
    )

    @mcp.tool(
        description=(
            "Search this surface's tools. Returns name + one-line brief; call "
            "describe_tool for arguments before run_tool."
        )
    )
    async def search_tools(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        async def work(scope):
            await catalogue.ensure_fresh()
            return search_hits(
                catalogue.index, catalogue.by_name, query, limit, published_names
            )

        return await pipeline.run("search_tools", work)

    @mcp.tool(description="Return the argument schema + summary for a tool name.")
    async def describe_tool(name: str) -> dict:
        async def work(scope):
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

        return await pipeline.run("describe_tool", work)

    @mcp.tool(description="Invoke any tool on this surface by name with an args object.")
    async def run_tool(name: str, args: dict | None = None) -> dict:
        async def work(scope):
            await catalogue.ensure_fresh()
            d = catalogue.by_name.get(name)
            if d is None:
                scope.inner_tool = UNKNOWN_TOOL  # caller input never becomes a label
                raise unknown_tool(name)
            scope.inner_tool = d.name
            return await scope.execute(d.verb, prepare_args(d, args or {}))

        return await pipeline.run("run_tool", work)

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

        async def work(scope):
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

        return await pipeline.run("context_cost", work)

    return mcp

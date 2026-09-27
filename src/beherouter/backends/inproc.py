"""inproc backend: an in-process FastMCP server behind the MCP pipeline.

⚠️ THE ONLY MODULE THAT IMPORTS FASTMCP FOR THIS BACKING (and for the `openapi`
and `python-dir` sources). FastMCP 4.0 moved the modules below, so keeping
every name here makes the 4.x port one file.

Listing reuses `backend_from_client` over an in-memory transport, so an
in-process tool gets exactly the pipeline every MCP backend has. Calls go
direct (`server.call_tool`): about 10x cheaper than a session per call, and the
one place a per-call identity is applied.

Three measured facts shape `InprocExecutor.run` (FastMCP 3.4.5, 2026-09-26;
the spec's § Measured 2026-09-26):

- Q1: nothing in FastMCP validates arguments for a non-function tool (an
  OpenAPI tool builds whatever request it can, including a literal
  `/customers/{id}`). So we validate first. Function tools validate and COERCE
  with pydantic, so they are left to it.
- `fastmcp.exceptions.ValidationError` is not a `ToolError`. Both are caller
  mistakes, so both map to `UsageError` — unless the ToolError merely wraps a
  crash or an upstream 5xx/network failure (`_is_outage`).
- Q3: an OpenAPI tool copies every header of the CURRENT INBOUND request onto
  its upstream request (`get_http_headers()`, minus `authorization` and a few
  others). Run in the gateway's request context, that forwards one backend's
  per-user credential to every OpenAPI surface. The call therefore runs in a
  BLANK `contextvars.Context` that holds only the identity we set.
"""

import asyncio
import contextvars
import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import httpx
import jsonschema
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import FastMCPError, NotFoundError, ToolError, ValidationError
from fastmcp.server.providers.filesystem_discovery import discover_and_import
from fastmcp.server.providers.openapi.routing import MCPType
from fastmcp.tools import Tool
from fastmcp.tools.function_tool import FunctionTool

from ..errors import Unavailable, UsageError
from ..models import Backend, ToolDescriptor
from .backing import McpBacking
from .mcp import _payload, backend_from_client

logger = logging.getLogger(__name__)

# The current call's identity headers. Set ONLY inside the blank context an
# InprocExecutor call runs in, so it can never leak between callers.
CURRENT_IDENTITY_HEADERS: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "beherouter_inproc_identity_headers", default=MappingProxyType({})
)

# Set on an httpx client by `identity_client`, and carried onto the server by
# `mark_identity_aware` (which `openapi_server` calls). `load_inproc_backend` reads it off the server.
IDENTITY_MARKER = "_beherouter_identity_aware"


def identity_client(**httpx_kwargs) -> httpx.AsyncClient:
    """An httpx client that puts the current call's identity headers OVER its
    attach-time headers — per request, never mutating shared state.

    Runs as a request event hook, i.e. after FastMCP has merged its own
    headers, so the identity wins over both the attach-time default and anything
    FastMCP copied. Marked so the gateway can tell an identity-aware source from
    one that would silently call as the deployment.
    """

    async def _apply(request: httpx.Request) -> None:
        for name, value in CURRENT_IDENTITY_HEADERS.get().items():
            request.headers[name] = value

    hooks = dict(httpx_kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [*hooks.get("request", []), _apply]
    client = httpx.AsyncClient(event_hooks=hooks, **httpx_kwargs)
    setattr(client, IDENTITY_MARKER, True)
    return client


def mark_identity_aware(server: FastMCP, client: httpx.AsyncClient) -> FastMCP:
    """Mark `server` as applying the caller's identity through `client`.

    The public way for a decorator-path plugin to be per-user: its tools call
    out through `client`, which must come from `identity_client`. Any other
    client is refused, because a marked server whose client ignores
    CURRENT_IDENTITY_HEADERS would be believed per-user and call as the
    deployment. Returns `server` for chaining.
    """
    if not getattr(client, IDENTITY_MARKER, False):
        raise UsageError(
            "mark_identity_aware needs a client built by identity_client(); "
            "any other client would call as the deployment"
        )
    setattr(server, IDENTITY_MARKER, True)
    return server


def _server(backing: McpBacking) -> FastMCP:
    if not isinstance(backing.server, FastMCP):
        raise UsageError(
            f"'{backing.name}': an inproc backing needs `server`, a FastMCP "
            f"instance built by the plugin"
        )
    return backing.server


class InprocExecutor:
    def __init__(self, backing: McpBacking) -> None:
        self._backing = backing
        self._server = _server(backing)
        # Whether this server's outbound client applies CURRENT_IDENTITY_HEADERS.
        # The gateway refuses an identity surface when it does not.
        self.identity_aware = bool(getattr(self._server, IDENTITY_MARKER, False))

    async def run(self, verb: str, args: dict, *, identity=None) -> dict:
        if self._backing.guard is not None:
            self._backing.guard(verb, args)
        headers = dict(identity.headers) if identity is not None and identity.headers else {}

        async def _call():
            CURRENT_IDENTITY_HEADERS.set(headers)
            tool = await self._server.get_tool(verb)
            if tool is None:
                raise UsageError(f"backend has no tool '{verb}'")
            if not isinstance(tool, FunctionTool):
                try:
                    jsonschema.validate(args, tool.parameters)
                except jsonschema.ValidationError as e:
                    where = e.json_path if e.path else "arguments"
                    raise UsageError(f"backend rejected '{verb}': {where}: {e.message}") from e
            return await self._server.call_tool(verb, args)

        try:
            # A blank context: no inbound request, no auth token, only `headers`.
            res = await asyncio.create_task(_call(), context=contextvars.Context())
        except UsageError:
            raise
        except (ToolError, ValidationError, NotFoundError) as e:
            if _is_outage(e):
                raise Unavailable(f"backend call '{verb}' failed: {e}") from e
            raise UsageError(f"backend rejected '{verb}': {e}") from e
        except Exception as e:
            raise Unavailable(f"backend call '{verb}' failed: {e}") from e
        return {"result": _unwrapped(res)}


def _unwrapped(res):
    """The call's value, with FastMCP's envelope for a non-object return removed.

    A tool returning `str`/`int`/`list` is sent as structured `{"result": v}`
    and marked `meta.fastmcp.wrap_result`; the Client session unwraps it, so an
    http/stdio backend yields `v`. Calling the server directly skips that, and
    this backing then wrapped once more: `{"result": {"result": v}}` (found by
    python-dir, whose functions mostly return plain values). Keyed on the
    marker, never on the shape, so a tool that really returns `{"result": …}`
    keeps it.
    """
    structured = getattr(res, "structured_content", None)
    wrapped = ((getattr(res, "meta", None) or {}).get("fastmcp") or {}).get("wrap_result")
    if wrapped and isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    return _payload(res)


def _is_outage(e: Exception) -> bool:
    """Whether a FastMCP error is really a backend fault, not a caller mistake.

    `server.call_tool` passes a tool's own ToolError through, but re-raises ANY
    other exception as `ToolError(...) from e` (3.4.5), so the type alone
    would file a crashed tool as a bad call. The cause chain tells them apart.
    An OpenAPI tool turns every upstream failure into a ValueError whose cause
    is the httpx error: a 4xx stays the caller's to correct, while a 5xx or a
    network failure is an outage.
    """
    if not isinstance(e, ToolError):
        return False  # ValidationError / NotFoundError: always the caller's
    cause = e.__cause__
    if cause is None or isinstance(cause, FastMCPError):
        return False
    while cause is not None:
        if isinstance(cause, httpx.HTTPStatusError):
            return cause.response.status_code >= 500
        if isinstance(cause, httpx.RequestError):
            return True
        cause = cause.__cause__
    return True


async def _list(backing: McpBacking, server: FastMCP) -> Backend:
    async with Client(FastMCPTransport(server)) as client:
        return await backend_from_client(
            backing.name,
            client,
            backing.pinned,
            backing.republish_output_schema,
            backing.notes,
        )


async def load_inproc_backend(backing: McpBacking) -> Backend:
    """List an in-process server through the MCP pipeline; call it directly."""
    server = _server(backing)
    try:
        backend = await _list(backing, server)
    except UsageError:
        raise
    except Exception as e:
        raise Unavailable(f"could not attach inproc backend '{backing.name}': {e}") from e
    backend.kind = "inproc"
    backend.executor = InprocExecutor(backing)

    async def _relist() -> list[ToolDescriptor]:
        return (await _list(backing, server)).descriptors

    backend.relist = _relist
    return backend


def _include_filter(include: frozenset[str]):
    """A TOTAL route filter: no lookup here can raise. It has to be total,
    because FastMCP swallows an exception from it and publishes the route."""

    def fn(route, route_type):
        return route_type if route.operation_id in include else MCPType.EXCLUDE

    return fn


def _close_schema(route, component) -> None:
    """additionalProperties: false, so `args.prepare_args` refuses an
    undeclared argument with a did-you-mean instead of the upstream dropping it."""
    component.parameters = {**component.parameters, "additionalProperties": False}


async def openapi_server(
    document: dict, *, client: httpx.AsyncClient, include: list[str] | None, name: str = "openapi"
) -> FastMCP:
    """A FastMCP server for `include`'s operations (all of them for None).

    ⚠️ Both FastMCP hooks used here FAIL OPEN (3.4.5: an exception in either is
    logged and ignored), so the result is checked after construction, through
    the public `list_tools()`: the published names must equal `include`, and
    every schema must be closed. Async for exactly that reason.
    """
    server = FastMCP.from_openapi(
        document,
        client=client,
        name=name,
        route_map_fn=_include_filter(frozenset(include)) if include is not None else None,
        mcp_component_fn=_close_schema,
    )
    tools = {t.name: t for t in await server.list_tools()}
    if include is not None and set(tools) != set(include):
        missing = sorted(set(include) - set(tools))
        extra = sorted(set(tools) - set(include))
        raise UsageError(
            f"OpenAPI include does not match what was published: "
            f"missing {missing}, unexpected {extra}"
        )
    open_ = sorted(n for n, t in tools.items() if t.parameters.get("additionalProperties") is not False)
    if open_:
        raise UsageError(f"OpenAPI tool schema(s) not closed: {open_}")
    if getattr(client, IDENTITY_MARKER, False):
        mark_identity_aware(server, client)
    return server


def python_dir_server(path: str | Path, *, name: str) -> FastMCP:
    """A FastMCP server for the `@tool` functions in the `.py` files under `path`.

    ⚠️ Deliberately NOT `FileSystemProvider`, which fails open three ways
    (FastMCP 3.4.5): a file that fails to import is logged and skipped, a
    missing root only warns, and it is built `on_duplicate="replace"`, so a
    second file defining the same tool silently wins. Each of those is a
    surface that attaches green and serves something other than what the
    operator wrote. This runs the same discovery and refuses all three.

    Duplicates are compared by the wrapped FUNCTION: discovery scans each
    module's namespace and builds a fresh `Tool` per sighting, so a tool one
    file imports from another is seen twice, and that is not a conflict.
    """
    root = Path(path)
    if not root.is_dir():
        raise UsageError(f"'{name}': python-dir path '{root}' is not a directory")
    result = discover_and_import(root)
    if result.failed_files:
        failures = "; ".join(f"{p}: {err}" for p, err in sorted(result.failed_files.items()))
        raise UsageError(f"'{name}': python-dir could not import {failures}")
    server = FastMCP(name)
    seen: dict[str, tuple[object, Path]] = {}
    dropped: list[str] = []
    for file, component in result.components:
        if not isinstance(component, Tool):
            dropped.append(f"{file}: {component.name}")
            continue
        fn = getattr(component, "fn", component)
        prior = seen.get(component.name)
        if prior is not None:
            if prior[0] is not fn:
                raise UsageError(
                    f"'{name}': tool '{component.name}' is defined in both "
                    f"{prior[1]} and {file}; rename one"
                )
            continue
        seen[component.name] = (fn, file)
        server.add_tool(component)
    if dropped:
        logger.warning(
            "'%s': python-dir surfaces tools only; ignoring %s", name, ", ".join(dropped)
        )
    if not seen:
        raise UsageError(
            f"'{name}': python-dir path '{root}' defines no tools; decorate "
            f"functions with @tool (from fastmcp.tools import tool)"
        )
    return server

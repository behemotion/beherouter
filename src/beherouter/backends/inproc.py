"""inproc backend: an in-process FastMCP server behind the MCP pipeline.

⚠️ THE ONLY MODULE THAT IMPORTS FASTMCP FOR THIS BACKING (and for the `openapi`
source). FastMCP 4.0 moved the modules below, so keeping every name here makes
the 4.x port one file.

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
from collections.abc import Mapping
from types import MappingProxyType

import httpx
import jsonschema
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import FastMCPError, NotFoundError, ToolError, ValidationError
from fastmcp.server.providers.openapi.routing import MCPType
from fastmcp.tools.function_tool import FunctionTool

from ..errors import Unavailable, UsageError
from ..models import Backend, ToolDescriptor
from .backing import McpBacking
from .mcp import _payload, backend_from_client

# The current call's identity headers. Set ONLY inside the blank context an
# InprocExecutor call runs in, so it can never leak between callers.
CURRENT_IDENTITY_HEADERS: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "beherouter_inproc_identity_headers", default=MappingProxyType({})
)

# Set on an httpx client by `identity_client`, and carried onto the server by
# `openapi_server`. `load_inproc_backend` reads it off the server.
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
        return {"result": _payload(res)}


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
    setattr(server, IDENTITY_MARKER, bool(getattr(client, IDENTITY_MARKER, False)))
    return server

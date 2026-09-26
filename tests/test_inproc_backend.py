"""The `inproc` backing: an in-process FastMCP server behind the MCP pipeline."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from beherouter.backends.backing import McpBacking
from beherouter.backends.inproc import IDENTITY_MARKER, identity_client, load_inproc_backend
from beherouter.errors import Unavailable, UsageError
from beherouter.plugins.spec import BACKINGS


def _server() -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool(annotations={"readOnlyHint": True})
    def lookup_order(order_id: str, count: int = 1) -> dict:
        """Fetch one order."""
        return {"order_id": order_id, "count": count}

    @mcp.tool
    def explode(reason: str) -> str:
        """Always fails."""
        from fastmcp.exceptions import ToolError

        raise ToolError(f"refused: {reason}")

    @mcp.tool
    def crash() -> str:
        """Raises a non-tool error."""
        raise RuntimeError("upstream fell over")

    return mcp


def _backing(server, **kw) -> McpBacking:
    return McpBacking(name="acme", transport="inproc", server=server, **kw)


def test_inproc_is_a_backing():
    assert "inproc" in BACKINGS


async def test_listing_uses_the_mcp_pipeline():
    backend = await load_inproc_backend(
        _backing(_server(), pinned=["lookup_order"], notes={"lookup_order": "Orders only."})
    )
    assert backend.kind == "inproc"
    by_name = {d.name: d for d in backend.descriptors}
    assert set(by_name) == {"lookup_order", "explode", "crash"}
    assert by_name["lookup_order"].pinned is True
    assert by_name["explode"].pinned is False
    assert by_name["lookup_order"].summary.endswith("Orders only.")
    assert by_name["lookup_order"].mutating is False
    assert by_name["lookup_order"].schema["required"] == ["order_id"]


async def test_relist_sees_a_tool_added_after_attach():
    server = _server()
    backend = await load_inproc_backend(_backing(server))

    @server.tool
    def late() -> str:
        """Added later."""
        return "x"

    names = {d.name for d in await backend.relist()}
    assert "late" in names


async def test_a_call_returns_the_payload():
    backend = await load_inproc_backend(_backing(_server()))
    out = await backend.executor.run("lookup_order", {"order_id": "o-1"})
    assert out == {"result": {"order_id": "o-1", "count": 1}}


async def test_a_function_tool_still_coerces_like_the_session_path():
    """Measured 2026-09-26: pydantic accepts "3" for an int on both FastMCP
    paths. Pre-validating function tools with jsonschema would refuse it."""
    backend = await load_inproc_backend(_backing(_server()))
    out = await backend.executor.run("lookup_order", {"order_id": "o", "count": "3"})
    assert out["result"]["count"] == 3


@pytest.mark.parametrize(
    "args",
    [{"order_id": 5}, {}, {"order_id": "o", "bogus": 1}],
    ids=["wrong-type", "missing", "extra"],
)
async def test_a_function_tool_validation_error_is_a_usage_error(args):
    """fastmcp.exceptions.ValidationError is NOT a ToolError subclass; mapped
    as 'anything else' it would read as a transient outage."""
    backend = await load_inproc_backend(_backing(_server()))
    with pytest.raises(UsageError, match="lookup_order"):
        await backend.executor.run("lookup_order", args)


async def test_a_tool_error_is_a_usage_error():
    backend = await load_inproc_backend(_backing(_server()))
    with pytest.raises(UsageError, match="refused: nope"):
        await backend.executor.run("explode", {"reason": "nope"})


async def test_any_other_failure_is_unavailable():
    backend = await load_inproc_backend(_backing(_server()))
    with pytest.raises(Unavailable, match="crash"):
        await backend.executor.run("crash", {})


async def test_an_unknown_tool_is_a_usage_error():
    backend = await load_inproc_backend(_backing(_server()))
    with pytest.raises(UsageError, match="no_such_tool"):
        await backend.executor.run("no_such_tool", {})


async def test_the_guard_runs_before_the_call():
    calls = []

    def guard(verb, args):
        calls.append(verb)
        raise UsageError("not on this edition")

    backend = await load_inproc_backend(_backing(_server(), guard=guard))
    with pytest.raises(UsageError, match="not on this edition"):
        await backend.executor.run("lookup_order", {"order_id": "o"})
    assert calls == ["lookup_order"]


async def test_a_non_function_tool_is_validated_against_its_schema():
    """The OpenAPI case (Q1): nothing in FastMCP validates such a tool, so the
    executor must — a bad call must never reach the upstream."""
    from fastmcp.tools import Tool
    from fastmcp.tools.base import ToolResult

    seen = []

    class RawTool(Tool):
        async def run(self, arguments):
            seen.append(arguments)
            return ToolResult(structured_content={"ok": True})

    server = FastMCP("raw")
    server.add_tool(
        RawTool(
            name="get_customer",
            description="Get one.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "maximum": 50}},
                "required": ["id"],
                "additionalProperties": False,
            },
        )
    )
    backend = await load_inproc_backend(_backing(server))
    for bad in ({"id": "x"}, {}, {"id": 500}, {"id": 1, "extra": 2}):
        with pytest.raises(UsageError, match="get_customer"):
            await backend.executor.run("get_customer", bad)
    assert seen == []
    assert (await backend.executor.run("get_customer", {"id": 7}))["result"] == {"ok": True}


async def test_an_inproc_backing_without_a_server_is_refused():
    with pytest.raises(UsageError, match="acme.*server"):
        await load_inproc_backend(McpBacking(name="acme", transport="inproc"))


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, UsageError), (422, UsageError), (502, Unavailable)],
)
async def test_an_upstream_http_error_is_classified_by_status(status, expected):
    """OpenAPITool raises ValueError(...) from httpx.HTTPStatusError, and
    call_tool wraps THAT in a ToolError: the status decides, not the type."""
    server = FastMCP("up")

    @server.tool
    def fetch() -> str:
        """Calls an upstream that fails."""
        req = httpx.Request("GET", "https://up/x")
        err = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(status, request=req))
        raise ValueError(f"HTTP error {status}") from err

    backend = await load_inproc_backend(_backing(server))
    with pytest.raises(expected, match="fetch"):
        await backend.executor.run("fetch", {})


def _upstream():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"auth": req.headers.get("authorization")})

    return seen, httpx.MockTransport(handler)


def _http_server(client: httpx.AsyncClient) -> FastMCP:
    mcp = FastMCP("up")

    @mcp.tool
    async def whoami() -> dict:
        """Ask the upstream who we are."""
        return (await client.get("/me")).json()

    setattr(mcp, IDENTITY_MARKER, getattr(client, IDENTITY_MARKER, False))
    return mcp


def _ident(**headers):
    return SimpleNamespace(headers=headers)


async def test_identity_client_applies_the_call_identity_over_the_default():
    _seen, transport = _upstream()
    client = identity_client(
        base_url="https://up", transport=transport, headers={"authorization": "Bearer deploy"}
    )
    backend = await load_inproc_backend(_backing(_http_server(client)))
    assert backend.executor.identity_aware is True
    alice = await backend.executor.run("whoami", {}, identity=_ident(authorization="Bearer alice"))
    shared = await backend.executor.run("whoami", {})
    assert alice["result"] == {"auth": "Bearer alice"}
    assert shared["result"] == {"auth": "Bearer deploy"}


async def test_concurrent_calls_each_see_only_their_own_identity():
    _seen, transport = _upstream()
    client = identity_client(base_url="https://up", transport=transport)
    backend = await load_inproc_backend(_backing(_http_server(client)))
    who = ["alice", "bob", "carol"]
    outs = await asyncio.gather(
        *(backend.executor.run("whoami", {}, identity=_ident(authorization=f"Bearer {w}")) for w in who)
    )
    assert [o["result"]["auth"] for o in outs] == [f"Bearer {w}" for w in who]


async def test_the_identity_does_not_outlive_a_failed_call():
    from beherouter.backends.inproc import CURRENT_IDENTITY_HEADERS

    backend = await load_inproc_backend(_backing(_server()))
    with pytest.raises(Unavailable):
        await backend.executor.run("crash", {}, identity=_ident(authorization="Bearer alice"))
    assert CURRENT_IDENTITY_HEADERS.get() == {}


async def test_a_server_without_an_identity_client_is_not_identity_aware():
    backend = await load_inproc_backend(_backing(_server()))
    assert backend.executor.identity_aware is False


async def test_the_gateway_refuses_identity_on_a_source_that_cannot_apply_it(monkeypatch):
    """The stdio rule restated for inproc: never 'believed per-user, actually
    shared'."""
    from beherouter.gateway import build_surfaces
    from beherouter.plugins import PLUGINS, register
    from beherouter.plugins.spec import IdentitySupport, PluginSpec
    from beherouter.registry import RegistryEntry

    spec = PluginSpec(
        name="t-inproc-blind",
        summary="t",
        backing="inproc",
        identity=IdentitySupport(modes=("client",), target="header"),
    )

    async def build(ctx):
        return await load_inproc_backend(_backing(_server()))

    register(spec, build)
    try:
        entry = RegistryEntry(
            name="blind",
            plugin="t-inproc-blind",
            identity={"mode": "client", "map": {"authorization": "x-token"}},
        )
        with pytest.raises(UsageError, match="blind.*cannot apply"):
            await build_surfaces({"blind": entry})
    finally:
        PLUGINS.pop("t-inproc-blind", None)


async def test_the_gateway_callers_headers_never_reach_the_upstream():
    """Q3: FastMCP's OpenAPI tool merges get_http_headers() — the INBOUND
    request's headers — into its upstream request. Run from inside the gateway's
    request, that leaked a caller's x-api-key and cookie upstream."""
    from fastmcp import Client as McpClient
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.dependencies import get_http_headers

    seen, transport = _upstream()
    client = identity_client(base_url="https://up", transport=transport)
    inner = FastMCP("inner")

    @inner.tool
    async def whoami() -> dict:
        """Forward the inbound headers the way OpenAPITool.run does."""
        return (await client.get("/me", headers=get_http_headers())).json()

    setattr(inner, IDENTITY_MARKER, True)
    backend = await load_inproc_backend(_backing(inner))
    outer = FastMCP("gateway")

    @outer.tool
    async def run_tool() -> dict:
        return await backend.executor.run("whoami", {}, identity=_ident(authorization="Bearer alice"))

    app = outer.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        asgi = httpx.ASGITransport(app=app)
        t = StreamableHttpTransport(
            "http://gw/mcp",
            headers={"x-api-key": "caller-pat", "cookie": "s=caller", "authorization": "Bearer gw"},
            httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=asgi, **kw),
        )
        async with McpClient(t) as c:
            await c.call_tool("run_tool", {})
    up = seen[-1].headers
    assert up["authorization"] == "Bearer alice"
    assert "x-api-key" not in up and "cookie" not in up
    assert not [k for k in up if k.startswith("mcp-")]

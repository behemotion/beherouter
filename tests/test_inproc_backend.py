"""The `inproc` backing: an in-process FastMCP server behind the MCP pipeline."""

import pytest
from fastmcp import FastMCP

from beherouter.backends.backing import McpBacking
from beherouter.backends.inproc import load_inproc_backend
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
    import httpx

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

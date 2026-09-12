import pytest
from fastmcp import Client, FastMCP

from beherouter.backends.backing import McpBacking
from beherouter.backends.mcp import (
    MCPClientExecutor,
    backend_from_client,
    build_transport,
)
from beherouter.errors import Unavailable, UsageError


@pytest.fixture
def fake_server():
    s = FastMCP("fakegitea")

    @s.tool
    def list_issues(repo: str) -> str:
        """List issues in a repo"""
        return f"issues for {repo}"

    @s.tool
    def create_repo(name: str) -> str:
        """Create a new repository"""
        return f"created {name}"

    return s


async def test_backend_from_client_lists_tools(fake_server):
    async with Client(fake_server) as c:
        b = await backend_from_client("fakegitea", c, pinned=["list_issues"])
    assert {d.name for d in b.descriptors} == {"list_issues", "create_repo"}
    assert [d.name for d in b.pinned] == ["list_issues"]
    assert b.kind == "mcp"


async def test_backend_descriptors_carry_schema_and_summary(fake_server):
    async with Client(fake_server) as c:
        b = await backend_from_client("fakegitea", c)
    d = {x.name: x for x in b.descriptors}["list_issues"]
    assert "issues" in d.summary.lower()
    assert d.schema["properties"]["repo"]["type"] == "string"


async def test_no_pinned_means_nothing_pinned(fake_server):
    """mcp backends have no manifest 'pinned' flag — the registry decides."""
    async with Client(fake_server) as c:
        b = await backend_from_client("fakegitea", c)
    assert b.pinned == []


async def test_mcp_executor_forwards(fake_server):
    async with Client(fake_server) as c:
        ex = MCPClientExecutor(c)
        out = await ex.run("list_issues", {"repo": "acme/x"})
    assert "acme/x" in str(out)


async def test_unknown_tool_is_a_usage_error(fake_server):
    """An unknown tool name is a caller mistake, not a backend outage.

    This test previously asserted Unavailable. That conflated a bad argument
    with a dead subprocess, and Unavailable is a CONVENTIONS-level
    classification with a reserved exit code -- so a backend UsageError
    arriving as Unavailable reads to every monitor as an outage.
    """
    async with Client(fake_server) as c:
        ex = MCPClientExecutor(c)
        with pytest.raises(UsageError):
            await ex.run("no_such_tool", {})


async def test_backend_tool_error_is_a_usage_error():
    """isError:true is a successful MCP response the model should self-correct
    against -- the doc's Error Handling case."""
    s = FastMCP("boomer")

    @s.tool
    def boom(x: str) -> str:
        """Always fails"""
        raise ValueError("action 'create' does not take: archive")

    async with Client(s) as c:
        ex = MCPClientExecutor(c)
        with pytest.raises(UsageError) as caught:
            await ex.run("boom", {"x": "1"})
    assert "does not take: archive" in str(caught.value)


async def test_transport_failure_is_unavailable():
    """A dead backend is still an outage. The split must not swallow that."""
    from beherouter.backends.backing import McpBacking
    from beherouter.backends.mcp import ReconnectingMCPExecutor, build_transport

    transport = build_transport(
        McpBacking(name="dead", transport="http", url="http://127.0.0.1:1/mcp")
    )
    ex = ReconnectingMCPExecutor(transport)
    with pytest.raises(Unavailable):
        await ex.run("anything", {})


def test_build_transport_stdio():
    t = build_transport(
        McpBacking(name="g", transport="stdio", cmd="gitea-mcp --x")
    )
    assert t is not None


def test_build_transport_http():
    t = build_transport(
        McpBacking(name="p", transport="http", url="http://x/mcp")
    )
    assert t is not None


def test_build_transport_rejects_bad_backing():
    with pytest.raises(UsageError):
        build_transport(McpBacking(name="x", transport="smoke-signal"))


# --- stdio credential passthrough -------------------------------------------
#
# The MCP SDK scrubs a stdio subprocess's environment down to a 6-var safe list
# (HOME/LOGNAME/PATH/SHELL/TERM/USER), so a backend's credentials reach it ONLY
# if the transport is given an explicit `env`. Without these, a Gitea surface
# lists and searches perfectly while every actual tool call fails with
# "token is required" — the failure mode is invisible to a tool-list smoke test.


def test_stdio_transport_carries_backing_env():
    t = build_transport(
        McpBacking(
            name="g",
            transport="stdio",
            cmd="gitea-mcp --transport stdio",
            env={"GITEA_HOST": "http://gitea.example", "GITEA_ACCESS_TOKEN": "t0ken"},
        )
    )
    assert t.env["GITEA_ACCESS_TOKEN"] == "t0ken"
    assert t.env["GITEA_HOST"] == "http://gitea.example"


def test_stdio_transport_keeps_the_default_safe_env():
    """Entry env must ADD to the SDK's safe list, not replace it — dropping PATH
    would leave the subprocess unable to find its own helper binaries."""
    t = build_transport(
        McpBacking(
            name="g", transport="stdio", cmd="x", env={"TOKEN": "v"}
        )
    )
    assert "PATH" in t.env


def test_no_env_on_backing_leaves_transport_default():
    t = build_transport(McpBacking(name="g", transport="stdio", cmd="x"))
    assert t.env is None


def test_http_transport_carries_backing_env_as_headers():
    """http backends carry their credentials as request headers.

    The backing arrives ALREADY RESOLVED — `${VAR}` expansion moved up into the
    load path so every backing shares one rule (tests/test_envexpand.py).
    """
    t = build_transport(
        McpBacking(
            name="p",
            transport="http",
            url="http://x/mcp",
            env={"X-API-Key": "pk-1"},
        )
    )
    assert t.headers["X-API-Key"] == "pk-1"


# --- payload extraction: `.data` is not always populated ----------------------


class _Res:
    """Stand-in for fastmcp's CallToolResult, whose `.data` is None whenever the
    backend tool has no output schema."""

    def __init__(self, data=None, structured_content=None, content=None):
        self.data = data
        self.structured_content = structured_content
        self.content = content or []


class _Blob:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    def __init__(self, res):
        self._res = res

    async def call_tool(self, verb, args):
        return self._res


async def test_executor_falls_back_to_structured_content():
    """LIVE BUG 2026-08-04: office-mcp's `discover` returned its payload as
    structured_content with `.data` None, so the gateway forwarded
    {"result": null} and every call looked successful but empty."""
    payload = {"tools": [{"name": "pdf_merge"}]}
    ex = MCPClientExecutor(_FakeClient(_Res(data=None, structured_content=payload)))
    assert await ex.run("discover", {}) == {"result": payload}


async def test_executor_falls_back_to_text_content():
    ex = MCPClientExecutor(
        _FakeClient(_Res(data=None, structured_content=None, content=[_Blob("hello")]))
    )
    assert await ex.run("t", {}) == {"result": "hello"}


async def test_executor_prefers_data_when_present():
    ex = MCPClientExecutor(
        _FakeClient(_Res(data={"a": 1}, structured_content={"b": 2}))
    )
    assert await ex.run("t", {}) == {"result": {"a": 1}}


async def test_executor_empty_result_stays_none():
    ex = MCPClientExecutor(_FakeClient(_Res()))
    assert await ex.run("t", {}) == {"result": None}


@pytest.fixture
def annotated_server():
    """A backend that annotates some tools and not others.

    The unannotated tool is the important one: it is the shape that made every
    Plane delete advertise as read-only.
    """
    s = FastMCP("annotated")

    @s.tool(annotations={"readOnlyHint": True})
    def list_things() -> str:
        """List things"""
        return "things"

    @s.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    def delete_thing(id: str) -> str:
        """Delete a thing"""
        return f"deleted {id}"

    @s.tool
    def unannotated(x: str) -> str:
        """No annotations at all"""
        return x

    return s


async def test_read_only_annotation_becomes_mutating_false(annotated_server):
    async with Client(annotated_server) as c:
        b = await backend_from_client("annotated", c)
    d = {x.name: x for x in b.descriptors}
    assert d["list_things"].mutating is False


async def test_destructive_annotation_becomes_mutating_true(annotated_server):
    """The gap this closes: every Plane delete advertised as non-mutating."""
    async with Client(annotated_server) as c:
        b = await backend_from_client("annotated", c)
    d = {x.name: x for x in b.descriptors}
    assert d["delete_thing"].mutating is True


async def test_unannotated_tool_is_mutating_none(annotated_server):
    """Unknown, not False. Absence of evidence is not evidence of safety."""
    async with Client(annotated_server) as c:
        b = await backend_from_client("annotated", c)
    d = {x.name: x for x in b.descriptors}
    assert d["unannotated"].mutating is None
    assert d["unannotated"].annotations is None


async def test_annotations_are_stored_as_plain_dicts(annotated_server):
    """Descriptors must not hold SDK pydantic objects: they are serialized by
    costing.py and compared in tests."""
    async with Client(annotated_server) as c:
        b = await backend_from_client("annotated", c)
    d = {x.name: x for x in b.descriptors}
    ann = d["delete_thing"].annotations
    assert isinstance(ann, dict)
    assert ann["destructiveHint"] is True
    assert "readOnlyHint" in ann


async def test_output_schema_is_carried(annotated_server):
    """FastMCP derives an outputSchema from the return annotation."""
    async with Client(annotated_server) as c:
        b = await backend_from_client("annotated", c)
    d = {x.name: x for x in b.descriptors}
    assert d["list_things"].output_schema is not None
    assert d["list_things"].output_schema["type"] == "object"


# --- relist: only the mcp backing can re-fetch its catalogue ----------------


async def test_mcp_backend_relister_refetches_the_catalogue(fake_server):
    """The relister must see the backend's CURRENT catalogue, not a snapshot."""
    async with Client(fake_server) as c:
        b = await backend_from_client("fake", c, pinned=["list_issues"])
        assert b.relist is not None
        again = await b.relist()
    assert {d.name for d in again} == {d.name for d in b.descriptors}


async def test_mcp_relister_preserves_the_pin_list(fake_server):
    async with Client(fake_server) as c:
        b = await backend_from_client("fake", c, pinned=["list_issues"])
        again = await b.relist()
    assert [d.name for d in again if d.pinned] == ["list_issues"]


async def test_a_tool_annotated_without_read_only_hint_is_mutating():
    """The security-relevant branch of `_mutating`'s `.get(..., False)` default.

    A backend may annotate SOMETHING without annotating `readOnlyHint` — the dict
    is non-empty, so the tri-state's `None` arm does not apply, and MCP's own
    spec default for a missing `readOnlyHint` is False. That makes the tool
    mutating, which is the safe reading: an unstated read-only claim is not a
    read-only claim.
    """
    s = FastMCP("partly-annotated")

    @s.tool(annotations={"openWorldHint": True})
    def reaches_the_internet(q: str) -> str:
        """Queries something out there"""
        return q

    async with Client(s) as c:
        b = await backend_from_client("partly", c)
    d = {x.name: x for x in b.descriptors}["reaches_the_internet"]
    assert d.annotations == {"openWorldHint": True}  # non-empty, no readOnlyHint
    assert d.mutating is True

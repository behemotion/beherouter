import asyncio

import pytest
from fastmcp import Client, FastMCP

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.backends.mcp import backend_from_client
from beherouter.surface import build_surface

META = {"search_tools", "describe_tool", "run_tool", "context_cost"}


@pytest.fixture
def cli_surface(fake_cli_cmd):
    b = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    return build_surface(b)


async def test_surface_registers_pinned_and_meta_tools(cli_surface):
    async with Client(cli_surface) as c:
        names = {t.name for t in await c.list_tools()}
    assert META <= names
    assert "faketool_search" in names  # pinned flat tool listed
    assert "faketool_shelf_create" not in names  # non-pinned: search only


async def test_pinned_tool_publishes_a_real_input_schema(cli_surface):
    """The whole point of synthesizing a signature: agents need the schema.

    A `**kwargs` tool (the plan's original shape) is rejected outright by
    FastMCP 3.x, and would advertise an empty schema even if accepted.
    """
    async with Client(cli_surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["faketool_search"]
    schema = tool.inputSchema
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]


async def test_search_tools_finds_long_tail(cli_surface):
    async with Client(cli_surface) as c:
        res = await c.call_tool("search_tools", {"query": "create shelf"})
    assert "faketool_shelf_create" in str(res.data)


async def test_describe_tool_returns_schema(cli_surface):
    async with Client(cli_surface) as c:
        res = await c.call_tool("describe_tool", {"name": "faketool_shelf_create"})
    data = res.data
    assert data["name"] == "faketool_shelf_create"
    assert data["mutating"] is True
    assert "name" in data["args"]


async def test_cli_describe_tool_reports_callable(cli_surface):
    async with Client(cli_surface) as c:
        res = await c.call_tool("describe_tool", {"name": "faketool_search"})
    assert res.data["callable"] is True
    assert "note" not in res.data


async def test_describe_tool_unknown_name(cli_surface):
    async with Client(cli_surface) as c:
        res = await c.call_tool("describe_tool", {"name": "nope"})
    assert "error" in res.data


async def test_search_tools_returns_descriptions(cli_surface):
    """An agent must not need a describe_tool round-trip per hit."""
    async with Client(cli_surface) as c:
        res = await c.call_tool("search_tools", {"query": "search"})
    hit = res.data[0]
    assert hit["name"] == "faketool_search"
    assert hit["summary"] == "search things"
    assert hit["pinned"] is True
    assert hit["mutating"] is False


async def test_cli_surface_tool_is_callable(cli_surface):
    """A cli-kind pinned verb executes end-to-end, not just lists."""
    async with Client(cli_surface) as c:
        res = await c.call_tool("faketool_search", {"query": "hello"})
    assert res.data == {"hits": ["hello"]}


async def test_run_tool_reaches_non_pinned_tool(cli_surface):
    """run_tool is how the long tail is invoked — it must resolve non-pinned names."""
    async with Client(cli_surface) as c:
        res = await c.call_tool(
            "run_tool", {"name": "faketool_shelf_create", "args": {"name": "x"}}
        )
    assert res.data == {"created": ["x"]}


async def test_zero_argument_mcp_tool_builds():
    """A no-arg MCP tool publishes a bare {"type": "object"} with no properties.

    4 of gitea-mcp's 50 real tools look like this. Treating it as the beheaxi
    arg-map dialect crashed on `'str' object has no attribute 'get'`.
    """
    from beherouter.models import Backend, ToolDescriptor

    class NullExecutor:
        async def run(self, verb, args):
            return {"verb": verb, "args": args}

    d = ToolDescriptor(
        name="get_version",
        verb="get_version",
        summary="get the server version",
        schema={"type": "object"},
        pinned=True,
        mutating=False,
    )
    surface = build_surface(
        Backend(name="g", kind="mcp", descriptors=[d], executor=NullExecutor())
    )
    async with Client(surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["get_version"]
        assert tool.inputSchema.get("properties", {}) == {}
        res = await c.call_tool("get_version", {})
    assert res.data["verb"] == "get_version"


async def test_flag_style_arg_names_become_valid_parameters():
    """beheaxi renders OPTIONAL args as `--flag-name` (describe.py:_arg_entry).

    `--kind` is not a valid Python identifier, so the synthesized signature must
    sanitize it — while still forwarding the backend's original wire name.
    """
    from beherouter.models import Backend, ToolDescriptor

    calls = {}

    class RecordingExecutor:
        async def run(self, verb, args):
            calls["verb"], calls["args"] = verb, args
            return {"ok": True}

    d = ToolDescriptor(
        name="demo_attach",
        verb="attach",
        summary="attach a thing",
        schema={
            "tool": {"name": "tool", "type": "string", "required": True},
            "--kind": {"name": "--kind", "type": "string", "required": False},
            "--dry-run": {"name": "--dry-run", "type": "boolean", "required": False},
        },
        pinned=True,
        mutating=True,
    )
    backend = Backend(
        name="demo", kind="mcp", descriptors=[d], executor=RecordingExecutor()
    )
    surface = build_surface(backend)

    async with Client(surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["demo_attach"]
        props = set(tool.inputSchema["properties"])
        assert props == {"tool", "kind", "dry_run"}
        await c.call_tool("demo_attach", {"tool": "x", "kind": "cli"})

    # forwarded under the backend's own names, not the sanitized ones
    assert calls["args"] == {"tool": "x", "--kind": "cli"}


# --- mcp-kind surfaces: schema comes from JSON Schema, not a beheaxi manifest ---


@pytest.fixture
async def mcp_backend():
    s = FastMCP("fakegitea")

    @s.tool
    def list_issues(repo: str, limit: int = 10) -> str:
        """List issues in a repo"""
        return f"{limit} issues for {repo}"

    async with Client(s) as c:
        return await backend_from_client("fakegitea", c, pinned=["list_issues"])


async def test_mcp_surface_pinned_tool_has_schema(mcp_backend):
    surface = build_surface(mcp_backend)
    async with Client(surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["list_issues"]
    props = tool.inputSchema["properties"]
    assert props["repo"]["type"] == "string"
    assert props["limit"]["type"] == "integer"
    assert tool.inputSchema["required"] == ["repo"]


async def test_unset_optionals_with_defaults_are_not_forwarded():
    """An omitted optional must reach the backend as OMITTED, not as its default.

    The synthesized signature puts each optional's upstream `default` in as the
    PYTHON default so the republished schema stays faithful. The cost is that
    Python then fills every unset optional before `_tool` ever sees kwargs, and
    the old body forwarded anything that was not None — so every call carried
    every default the schema declared.

    Harmless against gitea-mcp (`page: 1` is valid on every tool that has it),
    fatal against an ACTION-PARAMETERIZED tool, whose schema is the UNION of all
    its actions' parameters. Plane's `workitem` is the real case: it declares
    `archive` (default `True`) for the archive action, so `workitem(action=
    "create", name=...)` arrived at the backend carrying `archive=True` and was
    rejected outright:

        Error: action 'create' does not take: archive.

    Reads happened to survive, so the surface looked healthy while every write
    through it failed. Measured against Plane 2026-09-08.
    """
    from beherouter.models import Backend, ToolDescriptor

    calls = {}

    class RecordingExecutor:
        async def run(self, verb, args):
            calls["args"] = args
            return {"ok": True}

    d = ToolDescriptor(
        name="workitem",
        verb="workitem",
        summary="action-parameterized resource tool",
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "name": {"type": "string", "default": ""},
                "archive": {"type": "boolean", "default": True},
                "per_page": {"type": "integer", "default": 0},
            },
            "required": ["action"],
        },
        pinned=True,
        mutating=True,
    )
    surface = build_surface(
        Backend(name="p", kind="mcp", descriptors=[d], executor=RecordingExecutor())
    )
    async with Client(surface) as c:
        # The published schema must still advertise the upstream defaults --
        # that fidelity is why they are in the signature at all.
        tool = {t.name: t for t in await c.list_tools()}["workitem"]
        props = tool.inputSchema["properties"]
        assert props["archive"]["default"] is True
        assert props["per_page"]["default"] == 0

        await c.call_tool("workitem", {"action": "create", "name": "x"})

    assert calls["args"] == {"action": "create", "name": "x"}, (
        "unset optionals leaked to the backend: " f"{sorted(calls['args'])}"
    )


# --- Task 3: republish annotations and wrapped output schema ---


from beherouter.models import Backend, ToolDescriptor
from beherouter.surface import republished_annotations, wrapped_output_schema


class _Echo:
    async def run(self, verb, args):
        return {"result": {"entries": [verb, args]}}


def _backend(*descriptors):
    return Backend(
        name="anno", kind="mcp", descriptors=list(descriptors), executor=_Echo()
    )


def test_wrapped_output_schema_describes_the_result_envelope():
    """The envelope and the backend's schema must both be true.

    FastMCP validates output against a declared schema, so publishing the
    backend's schema verbatim while returning {"result": ...} would fail
    validation on the first call.
    """
    d = ToolDescriptor(
        name="t", verb="t", summary="s", schema={}, pinned=True, mutating=False,
        output_schema={"type": "object", "properties": {"entries": {"type": "array"}}},
    )
    wrapped = wrapped_output_schema(d)
    assert wrapped["type"] == "object"
    assert wrapped["required"] == ["result"]
    assert wrapped["properties"]["result"]["properties"]["entries"]["type"] == "array"


def test_no_backend_schema_means_no_published_schema():
    d = ToolDescriptor(
        name="t", verb="t", summary="s", schema={}, pinned=True, mutating=False
    )
    assert wrapped_output_schema(d) is None


def test_unknown_mutating_publishes_no_annotations():
    """beherouter never invents an annotation a backend did not make."""
    d = ToolDescriptor(
        name="t", verb="t", summary="s", schema={}, pinned=True, mutating=None
    )
    assert republished_annotations(d) is None


def test_known_hints_pass_through():
    d = ToolDescriptor(
        name="t", verb="t", summary="s", schema={}, pinned=True, mutating=True,
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": True},
    )
    out = republished_annotations(d)
    assert out["readOnlyHint"] is False
    assert out["destructiveHint"] is True
    assert out["idempotentHint"] is False
    assert out["openWorldHint"] is True


async def test_pinned_tool_publishes_annotations_on_the_wire():
    """The gap this closes: a host that never calls search_tools -- Claude Code
    sees pinned tools directly -- had no mutation signal at all."""
    d = ToolDescriptor(
        name="delete_thing", verb="delete_thing", summary="Delete a thing",
        schema={"type": "object", "properties": {"id": {"type": "string"}},
                "required": ["id"]},
        pinned=True, mutating=True,
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    surface = build_surface(_backend(d))
    async with Client(surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["delete_thing"]
    assert tool.annotations is not None
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is False


async def test_declared_output_schema_validates_a_real_call():
    """The wrapper is what makes the declaration true rather than a lie that
    fails validation on the first call."""
    d = ToolDescriptor(
        name="lister", verb="lister", summary="List",
        schema={"type": "object", "properties": {}},
        pinned=True, mutating=False,
        output_schema={"type": "object",
                       "properties": {"entries": {"type": "array"}}},
    )
    surface = build_surface(_backend(d))
    async with Client(surface) as c:
        tool = {t.name: t for t in await c.list_tools()}["lister"]
        assert tool.outputSchema["properties"]["result"]["type"] == "object"
        res = await c.call_tool("lister", {})
    assert "result" in (res.structured_content or {})


async def test_search_tools_reports_unknown_mutating_as_null():
    d = ToolDescriptor(
        name="mystery", verb="mystery", summary="mystery tool",
        schema={}, pinned=False, mutating=None,
    )
    surface = build_surface(_backend(d))
    async with Client(surface) as c:
        hits = (await c.call_tool("search_tools", {"query": "mystery"})).data
    assert hits[0]["mutating"] is None


# --- Task 6: context_cost meta-tool -----------------------------------------


async def test_context_cost_tool_reports_the_surface_it_lives_on(cli_surface):
    async with Client(cli_surface) as c:
        payload = (await c.call_tool("context_cost", {})).data
    assert payload["advertised"] >= payload["pinned"]
    assert payload["tokens_published"] == (
        payload["tokens_meta"] + payload["tokens_pinned"]
    )
    assert payload["exact"] is False
    assert "pct_published" not in payload


async def test_context_cost_tool_accepts_a_context_window(cli_surface):
    async with Client(cli_surface) as c:
        payload = (await c.call_tool("context_cost", {"context_window": 200000})).data
    assert 0 < payload["pct_published"] < 100
    assert payload["pct_naive"] > 0


async def test_context_cost_counts_itself(cli_surface):
    """The tool is part of the surface's overhead and must say so.

    A cost figure that excluded the reporting tool would understate the very
    number it exists to report.
    """
    async with Client(cli_surface) as c:
        tools = {t.name: t for t in await c.list_tools()}
        payload = (await c.call_tool("context_cost", {})).data
    assert payload["tokens_meta"] >= tool_tokens_of(tools, "context_cost")


def tool_tokens_of(tools, name):
    from beherouter.costing import estimate_tokens
    return estimate_tokens(tools[name].model_dump_json(exclude_none=True))


# --- Task 11: meta-tools read through a refreshing Catalogue ---------------


async def test_the_published_array_does_not_move_when_the_catalogue_does():
    """THE property this whole design exists to protect.

    The doc warns that adding or removing tool definitions mid-conversation
    invalidates the provider's prompt-prefix cache, and that the resulting miss
    can cost more than the definitions removed. So the searchable catalogue may
    churn; the published array may not.
    """
    from beherouter.models import Backend, ToolDescriptor

    def _d(name, pinned=False):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=pinned, mutating=None,
        )

    async def relist():
        return [_d("pinned_one", pinned=True), _d("old"), _d("cycle_create")]

    backend = Backend(
        name="s", kind="mcp",
        descriptors=[_d("pinned_one", pinned=True), _d("old")],
        executor=_Echo(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)
    async with Client(surface) as c:
        before = [t.model_dump_json(exclude_none=True) for t in await c.list_tools()]
        # force a refresh through the meta-tool path
        hits = (await c.call_tool("search_tools", {"query": "cycle"})).data
        after = [t.model_dump_json(exclude_none=True) for t in await c.list_tools()]

    assert [h["name"] for h in hits] == ["cycle_create"]  # the index DID refresh
    assert before == after  # and the array did NOT move


async def test_run_tool_reaches_a_tool_that_appeared_after_attach():
    """The intended win: the long tail refreshes while the array does not."""
    from beherouter.models import Backend, ToolDescriptor

    def _d(name):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=False, mutating=None,
        )

    class _VerbEcho:
        async def run(self, verb, args):
            return {"result": verb}

    async def relist():
        return [_d("old"), _d("brand_new")]

    backend = Backend(
        name="s", kind="mcp", descriptors=[_d("old")],
        executor=_VerbEcho(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)
    async with Client(surface) as c:
        out = (await c.call_tool("run_tool", {"name": "brand_new"})).data
    assert out["result"] == "brand_new"


async def test_context_cost_reports_the_live_catalogue_status():
    from beherouter.catalogue import STATUS_DRIFT
    from beherouter.models import Backend, ToolDescriptor

    def _d(name):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=False, mutating=None,
        )

    async def relist():
        return [_d("old"), _d("added_later")]

    backend = Backend(
        name="s", kind="mcp", descriptors=[_d("old")],
        executor=_Echo(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)
    async with Client(surface) as c:
        # A single round trip, so exactly one ensure_fresh() call happens and
        # there is no second real network round trip to race it: two separate
        # meta-tool calls each independently checking a 1ms TTL against real
        # wall-clock time would each see the TTL expired and relist again,
        # and a second relist against an already-fresh catalogue reports no
        # further drift -- flaky, not a property of the design.
        payload = (await c.call_tool("context_cost", {})).data
    assert payload["catalogue"] == STATUS_DRIFT
    assert payload["catalogue_drift"]["added"] == ["added_later"]


async def test_a_cli_surface_reports_catalogue_none(cli_surface):
    """No relister, so nothing is claimed about freshness."""
    from beherouter.catalogue import STATUS_NONE

    async with Client(cli_surface) as c:
        payload = (await c.call_tool("context_cost", {})).data
    assert payload["catalogue"] == STATUS_NONE


async def test_a_tool_missing_at_attach_is_reported_unpinned_even_if_configured_pinned():
    """A tool named in registry.toml's pin list but absent at attach (the
    documented Plane Community-Edition 404 shape) can arrive on a later
    refresh still carrying `pinned=True` -- the backend recomputes that flag
    against the CONFIGURED pin list on every re-list (backends/mcp.py), not
    against what was actually registered.

    Registration is frozen by design, so this tool is never added to the
    published `tools` array. A meta-tool reporting `pinned: true` for it would
    tell an agent something the array contradicts -- exactly the gap this
    task exists to close.
    """
    from beherouter.models import Backend, ToolDescriptor

    def _d(name, pinned=False):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=pinned, mutating=None,
        )

    async def relist():
        # The backend re-derives `pinned` from its configured pin list on
        # every re-list, so a late-arriving tool that IS on that list comes
        # back pinned=True even though it was never registered.
        return [_d("old", pinned=True), _d("late_arrival", pinned=True)]

    backend = Backend(
        name="s", kind="mcp",
        descriptors=[_d("old", pinned=True)],
        executor=_Echo(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)
    async with Client(surface) as c:
        hits = (await c.call_tool("search_tools", {"query": "late"})).data
        described = (await c.call_tool("describe_tool", {"name": "late_arrival"})).data
        names = {t.name for t in await c.list_tools()}

    assert "late_arrival" not in names  # registration really is frozen
    assert hits[0]["name"] == "late_arrival"
    assert hits[0]["pinned"] is False
    assert described["pinned"] is False


async def test_two_concurrent_meta_tool_calls_re_list_the_backend_once():
    """Four meta-tools share ONE Catalogue, so they race each other.

    Two sessions arriving past the TTL must produce one backend round trip, not
    two -- and the loser must not block behind the winner's network call. It
    serves the last-good catalogue instead, the same trade a failed re-list
    already makes.
    """
    from beherouter.models import Backend, ToolDescriptor

    def _d(name):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=False, mutating=None,
        )

    calls = []

    async def relist():
        calls.append(1)
        await asyncio.sleep(0.05)  # hold the in-flight window open
        return [_d("old"), _d("cycle_create")]

    backend = Backend(
        name="s", kind="mcp", descriptors=[_d("old")],
        executor=_Echo(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)

    async def call(tool, args):
        async with Client(surface) as c:
            return (await c.call_tool(tool, args)).data

    await asyncio.sleep(0.01)  # let the 1ms TTL expire
    searched, cost = await asyncio.gather(
        call("search_tools", {"query": "cycle"}),
        call("context_cost", {}),
    )
    assert calls == [1]
    assert isinstance(searched, list)  # the loser still answered
    assert cost["tokens_published"] > 0


async def test_a_failed_re_list_leaves_the_surface_answering_as_stale():
    """What an operator actually sees when a backend cannot be re-listed.

    `stale` was asserted only at the Catalogue level. The seam that matters is
    the surface: `context_cost` must REPORT the degrade, and `search_tools` must
    keep answering from the last good catalogue rather than dying with the
    re-list.
    """
    from beherouter.catalogue import STATUS_STALE
    from beherouter.models import Backend, ToolDescriptor

    def _d(name):
        return ToolDescriptor(
            name=name, verb=name, summary=f"does {name}", schema={},
            pinned=False, mutating=None,
        )

    async def relist():
        raise RuntimeError("backend went away")

    backend = Backend(
        name="s", kind="mcp", descriptors=[_d("cycle_create")],
        executor=_Echo(), relist=relist, ttl_ms=1,
    )
    surface = build_surface(backend)
    async with Client(surface) as c:
        hits = (await c.call_tool("search_tools", {"query": "cycle"})).data
        payload = (await c.call_tool("context_cost", {})).data

    assert [h["name"] for h in hits] == ["cycle_create"]  # still answering
    assert payload["catalogue"] == STATUS_STALE


async def test_a_costing_failure_crosses_the_boundary_as_an_axi_error(monkeypatch):
    """CONVENTIONS: every error crossing the gateway boundary is an AxiError.

    `context_cost` is deliberately UNGUARDED at the tool level — a failing tool
    call should keep surfacing its error to the caller rather than returning a
    green payload. So the fix is to classify the escape, not to swallow it.
    """
    from beherouter import costing
    from beherouter.errors import Unavailable
    from beherouter.models import Backend, ToolDescriptor

    async def boom(*a, **kw):
        raise RuntimeError("tokenizer exploded")

    monkeypatch.setattr(costing, "surface_cost", boom)

    backend = Backend(
        name="s", kind="mcp",
        descriptors=[ToolDescriptor(
            name="t", verb="t", summary="", schema={}, pinned=True, mutating=None,
        )],
        executor=_Echo(),
    )
    surface = build_surface(backend)
    tool = await surface.get_tool("context_cost")
    with pytest.raises(Unavailable, match="tokenizer exploded"):
        await tool.fn()


async def test_a_bad_context_window_stays_a_usage_error(monkeypatch):
    """The classifier must not relabel an AxiError that is already correct."""
    from beherouter.errors import UsageError
    from beherouter.models import Backend, ToolDescriptor

    backend = Backend(
        name="s", kind="mcp",
        descriptors=[ToolDescriptor(
            name="t", verb="t", summary="", schema={}, pinned=True, mutating=None,
        )],
        executor=_Echo(),
    )
    surface = build_surface(backend)
    tool = await surface.get_tool("context_cost")
    with pytest.raises(UsageError, match="context_window"):
        await tool.fn(context_window=-1)

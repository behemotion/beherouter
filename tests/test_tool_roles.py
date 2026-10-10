"""A7: a role list per tool, on top of the surface gate (call-gates spec §3)."""

from fastmcp import Client

import beherouter.identity as identity_mod
from beherouter.audit import AuditSink
from beherouter.gates import Gates, ToolRoleGate
from beherouter.identity import RequestIdentity
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.surface import build_surface


class Spy:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append(verb)
        return {"result": "ok"}


def _d(name, pinned=True):
    return ToolDescriptor(
        name=name, verb=name, summary=f"{name} a call record",
        schema={}, pinned=pinned, mutating=True,
    )


def _caller(monkeypatch, *, roles=None, shared=False):
    claims = {"sub": "alice"}
    if roles is not None:
        claims["realm_access"] = {"roles": list(roles)}
    req = RequestIdentity(
        shared=shared, subject=None if shared else "alice",
        claims={} if shared else claims, raw_token="t",
    )
    monkeypatch.setattr(identity_mod, "request_identity", lambda wanted=(): req)


def _surface(hide=True):
    spy = Spy()
    backend = Backend(
        name="bo", kind="mcp", executor=spy,
        descriptors=[_d("recategorize_call"), _d("list_calls"), _d("reprocess_call", pinned=False)],
    )
    roles = ToolRoleGate(
        surface="bo",
        tools={"recategorize_call": ("bo-write",), "reprocess_call": ("bo-write",)},
        roles_claim="realm_access.roles",
    )
    gates = Gates(stages=(roles,), roles=roles, hide=hide)
    return build_surface(backend, audit=AuditSink(enabled=False), gates=gates), spy


async def test_a_holder_calls_the_gated_tool(monkeypatch):
    _caller(monkeypatch, roles=["bo-write"])
    surface, spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("recategorize_call", {})
    assert not res.isError and spy.calls == ["recategorize_call"]


async def test_a_non_holder_is_refused_naming_the_missing_role(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("recategorize_call", {})
    meta = res.meta[META_KEY]
    assert meta["reason"] == "missing_role"
    assert meta["context"]["missing_roles"] == ["bo-write"]
    assert "tool 'recategorize_call' on surface 'bo'" in res.content[0].text
    assert spy.calls == []


async def test_a_shared_caller_keeps_every_ungated_tool(monkeypatch):
    _caller(monkeypatch, shared=True)
    surface, spy = _surface()
    async with Client(surface) as c:
        ok = await c.call_tool_mcp("list_calls", {})
        gated = await c.call_tool_mcp("recategorize_call", {})
    assert not ok.isError
    assert gated.meta[META_KEY]["reason"] == "unauthenticated"
    assert spy.calls == ["list_calls"]


async def test_run_tool_gates_its_inner_tool(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "reprocess_call", "args": {}})
    assert res.meta[META_KEY]["reason"] == "missing_role" and spy.calls == []


async def test_listing_and_search_hide_what_the_caller_cannot_run(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, _ = _surface()
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
        hits = (await c.call_tool("search_tools", {"query": "call record"})).data
    assert "recategorize_call" not in names and "list_calls" in names
    assert {h["name"] for h in hits} == {"list_calls"}


async def test_a_holder_sees_the_gated_tools(monkeypatch):
    _caller(monkeypatch, roles=["bo-write"])
    surface, _ = _surface()
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
    assert "recategorize_call" in names


async def test_hide_tools_false_lists_everything(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, _ = _surface(hide=False)
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
    assert "recategorize_call" in names


async def test_describe_tool_answers_missing_role_for_a_hidden_tool(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, _ = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("describe_tool", {"name": "reprocess_call"})
    assert res.meta[META_KEY]["reason"] == "missing_role"


async def test_an_unknown_tool_never_suggests_a_tool_hidden_from_the_caller(monkeypatch):
    """`reprocess_cal` is one letter off a gated tool the caller cannot see; the
    suggestion would name the tool the listing hides."""
    _caller(monkeypatch, roles=["other"])
    surface, _spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "reprocess_cal", "args": {}})
    meta = res.meta[META_KEY]
    assert meta["reason"] == "unknown_tool"
    assert "reprocess_call" not in meta.get("context", {}).get("suggestions", [])
    assert "reprocess_call" not in res.content[0].text


async def test_a_holder_is_still_offered_the_gated_suggestion(monkeypatch):
    _caller(monkeypatch, roles=["bo-write"])
    surface, _spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "reprocess_cal", "args": {}})
    assert "reprocess_call" in res.meta[META_KEY]["context"]["suggestions"]


async def test_a_negative_search_limit_returns_nothing_gated_or_not(monkeypatch):
    _caller(monkeypatch, roles=["bo-write"])
    for hide in (True, False):
        surface, _spy = _surface(hide=hide)
        async with Client(surface) as c:
            res = await c.call_tool("search_tools", {"query": "call", "limit": -1})
        assert res.structured_content == {"result": []}


async def test_hide_tools_false_leaves_describe_ungated(monkeypatch):
    _caller(monkeypatch, roles=["other"])
    surface, _spy = _surface(hide=False)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("describe_tool", {"name": "reprocess_call"})
    assert not res.isError


async def test_a_shared_caller_lists_only_the_ungated_tools(monkeypatch):
    _caller(monkeypatch, shared=True)
    surface, _spy = _surface()
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
    assert "list_calls" in names and "recategorize_call" not in names

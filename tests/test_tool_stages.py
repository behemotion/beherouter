"""Tool stages: the per-tool seam inside `scope.execute` (call-gates spec §2)."""

import asyncio

from fastmcp import Client

from beherouter.audit import AuditSink
from beherouter.errors import AuthError, tag
from beherouter.gates import Gates
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.surface import build_surface


class Recording:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append(verb)
        return {"result": {"ok": True}}


def _d(name, pinned=True):
    return ToolDescriptor(
        name=name, verb=name, summary=f"{name} things",
        schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        pinned=pinned, mutating=False,
    )


def _surface(*stages, call_timeout_s=None):
    ex = Recording()
    backend = Backend(
        name="sx", kind="mcp", executor=ex,
        descriptors=[_d("find"), _d("hidden_tool", pinned=False)],
    )
    surface = build_surface(
        backend, audit=AuditSink(enabled=False),
        call_timeout_s=call_timeout_s, gates=Gates(stages=tuple(stages)),
    )
    return surface, ex


async def test_a_stage_sees_the_pinned_descriptor_and_prepared_args():
    seen = []

    async def spy(scope, d, args):
        seen.append((scope.tool, d.name, args))

    surface, _ = _surface(spy)
    async with Client(surface) as c:
        await c.call_tool("find", {"q": "x"})
    assert seen == [("find", "find", {"q": "x"})]


async def test_run_tool_reaches_the_stage_with_its_inner_descriptor():
    seen = []

    async def spy(scope, d, args):
        seen.append((scope.tool, scope.inner_tool, d.name))

    surface, _ = _surface(spy)
    async with Client(surface) as c:
        await c.call_tool("run_tool", {"name": "hidden_tool", "args": {"q": "x"}})
    assert seen == [("run_tool", "hidden_tool", "hidden_tool")]


async def test_a_tagged_refusal_stops_the_call_before_the_backend():
    async def refuse(scope, d, args):
        raise tag(AuthError("no"), "missing_role", required_roles=["w"])

    surface, ex = _surface(refuse)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("find", {"q": "x"})
    assert res.isError and res.meta[META_KEY]["reason"] == "missing_role"
    assert ex.calls == []


async def test_an_untagged_auth_error_in_a_stage_is_a_gate_refusal():
    async def refuse(scope, d, args):
        raise AuthError("who are you")

    surface, _ = _surface(refuse)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("find", {"q": "x"})
    assert res.meta[META_KEY]["reason"] == "unauthenticated"


async def test_catalogue_meta_tools_run_no_tool_stage():
    seen = []

    async def spy(scope, d, args):
        seen.append(d.name)

    surface, _ = _surface(spy)
    async with Client(surface) as c:
        await c.call_tool("search_tools", {"query": "find"})
        await c.call_tool("describe_tool", {"name": "find"})
        await c.call_tool("context_cost", {})
    assert seen == []


async def test_stage_time_does_not_count_toward_the_call_timeout():
    async def slow(scope, d, args):
        await asyncio.sleep(0.2)

    surface, ex = _surface(slow, call_timeout_s=0.05)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("find", {"q": "x"})
    assert not res.isError and ex.calls == ["find"]

"""A12: a human confirms a mutating call, through MCP elicitation (call-gates spec §5)."""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from beherouter.audit import AuditSink
from beherouter.gates import ConfirmGate, Gates
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.surface import build_surface


class Spy:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append(verb)
        return {"result": "ok"}


def _d(name, mutating, pinned=True):
    schema = {"type": "object", "additionalProperties": True}
    return ToolDescriptor(name=name, verb=name, summary=name, schema=schema,
                          pinned=pinned, mutating=mutating)


def _surface(timeout_s=300.0, call_timeout_s=None):
    spy = Spy()
    backend = Backend(
        name="bo", kind="mcp", executor=spy,
        descriptors=[
            _d("write", True), _d("unknown", None), _d("read", False),
            _d("exempt_one", None), _d("deep_write", True, pinned=False),
        ],
    )
    gate = ConfirmGate("bo", frozenset({"exempt_one"}), timeout_s=timeout_s)
    gates = Gates(stages=(gate,), confirm=gate)
    surface = build_surface(
        backend, audit=AuditSink(enabled=False), gates=gates, call_timeout_s=call_timeout_s
    )
    return surface, spy


def test_what_needs_confirmation():
    gate = ConfirmGate("bo", frozenset({"exempt_one"}))
    assert gate.applies(_d("w", True)) and gate.applies(_d("u", None))
    assert not gate.applies(_d("r", False)) and not gate.applies(_d("exempt_one", None))


async def _decline(message, response_type, params, ctx):
    return ElicitResult(action="decline")


async def _cancel(message, response_type, params, ctx):
    return ElicitResult(action="cancel")


async def _say_no(message, response_type, params, ctx):
    return response_type(value=False)


async def test_a_client_without_elicitation_is_refused():
    surface, spy = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("write", {})
    meta = res.meta[META_KEY]
    assert meta["reason"] == "confirmation_required"
    assert meta["context"] == {"confirmation": "unsupported"}
    assert spy.calls == []


async def test_an_accepted_confirmation_runs_the_call():
    asked = []

    async def accept(message, response_type, params, ctx):
        asked.append(message)
        return response_type(value=True)

    surface, spy = _surface()
    async with Client(surface, elicitation_handler=accept) as c:
        res = await c.call_tool_mcp("unknown", {})
    assert not res.isError and spy.calls == ["unknown"]
    assert "'unknown' on surface 'bo'" in asked[0]


@pytest.mark.parametrize("handler", [_decline, _cancel, _say_no])
async def test_anything_but_yes_is_declined(handler):
    surface, spy = _surface()
    async with Client(surface, elicitation_handler=handler) as c:
        res = await c.call_tool_mcp("write", {})
    assert res.meta[META_KEY]["context"] == {"confirmation": "declined"}
    assert spy.calls == []


async def test_no_answer_in_time_is_a_timeout():
    async def hang(message, response_type, params, ctx):
        await asyncio.sleep(1)
        return response_type(value=True)

    surface, spy = _surface(timeout_s=0.05)
    async with Client(surface, elicitation_handler=hang) as c:
        res = await c.call_tool_mcp("write", {})
    assert res.meta[META_KEY]["context"] == {"confirmation": "timeout"}
    assert spy.calls == []


async def test_read_only_and_exempt_tools_are_not_asked():
    surface, spy = _surface()
    async with Client(surface) as c:  # no elicitation: any ask would refuse
        assert not (await c.call_tool_mcp("read", {})).isError
        assert not (await c.call_tool_mcp("exempt_one", {})).isError
    assert spy.calls == ["read", "exempt_one"]


async def test_run_tool_confirms_its_inner_tool():
    surface, _ = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "deep_write", "args": {}})
    assert res.meta[META_KEY]["reason"] == "confirmation_required"


async def test_the_human_is_not_on_the_call_timeout():
    async def slow_yes(message, response_type, params, ctx):
        await asyncio.sleep(0.2)
        return response_type(value=True)

    surface, spy = _surface(call_timeout_s=0.05)
    async with Client(surface, elicitation_handler=slow_yes) as c:
        res = await c.call_tool_mcp("write", {})
    assert not res.isError and spy.calls == ["write"]


async def test_describe_tool_says_when_a_call_needs_confirmation():
    surface, _ = _surface()
    async with Client(surface) as c:
        write = (await c.call_tool("describe_tool", {"name": "write"})).data
        read = (await c.call_tool("describe_tool", {"name": "read"})).data
    assert write["requires_confirmation"] is True
    assert "requires_confirmation" not in read


async def test_a_long_early_argument_cannot_hide_a_later_one():
    asked = []

    async def accept(message, response_type, params, ctx):
        asked.append(message)
        return response_type(value=True)

    surface, _ = _surface()
    args = {"a_body": "x" * 18000, "z_target": "prod-db-42"}
    async with Client(surface, elicitation_handler=accept) as c:
        await c.call_tool_mcp("run_tool", {"name": "deep_write", "args": args})
    assert "z_target" in asked[0] and "prod-db-42" in asked[0]
    assert "a_body" in asked[0]
    assert "truncated" in asked[0] and "chars)" in asked[0]
    assert len(asked[0]) < 3000


async def test_short_arguments_are_shown_whole_and_not_called_truncated():
    asked = []

    async def accept(message, response_type, params, ctx):
        asked.append(message)
        return response_type(value=True)

    surface, _ = _surface()
    async with Client(surface, elicitation_handler=accept) as c:
        await c.call_tool_mcp("run_tool", {"name": "deep_write", "args": {"id": 7}})
    assert '{"id": 7}' in asked[0] and "truncated" not in asked[0]


@pytest.mark.parametrize("content", [{"value": "maybe"}, {}, None])
async def test_a_malformed_accept_is_declined_not_internal(content):
    async def malformed(message, response_type, params, ctx):
        return ElicitResult(action="accept", content=content)

    surface, spy = _surface()
    async with Client(surface, elicitation_handler=malformed) as c:
        res = await c.call_tool_mcp("write", {})
    meta = res.meta[META_KEY]
    assert meta["reason"] == "confirmation_required"
    assert meta["context"] == {"confirmation": "declined"}
    assert "maybe" not in str(res.model_dump())
    assert spy.calls == []


async def test_arguments_reach_only_the_human():
    asked = []

    async def decline(message, response_type, params, ctx):
        asked.append(message)
        return ElicitResult(action="decline")

    async def accept(message, response_type, params, ctx):
        asked.append(message)
        return response_type(value=True)

    surface, _ = _surface()
    async with Client(surface, elicitation_handler=decline) as c:
        res = await c.call_tool_mcp(
            "run_tool", {"name": "deep_write", "args": {"secret": "xyz-sentinel"}}
        )
    text = " ".join(getattr(b, "text", "") for b in res.content)
    assert res.meta[META_KEY]["context"] == {"confirmation": "declined"}
    assert "xyz-sentinel" not in text
    assert "xyz-sentinel" not in json.dumps(res.meta, default=str)
    async with Client(surface, elicitation_handler=accept) as c:
        await c.call_tool_mcp(
            "run_tool", {"name": "deep_write", "args": {"secret": "xyz-sentinel"}}
        )
    assert "xyz-sentinel" in asked[-1]


async def test_confirm_mutating_leaves_the_published_tools_byte_identical():
    """The published `tools` array is frozen at attach and is what a host's
    prompt cache keys on: turning confirmation on must not change one byte of
    it (describe_tool is where `requires_confirmation` is reported)."""
    gated, _ = _surface()
    backend = Backend(
        name="bo", kind="mcp", executor=Spy(),
        descriptors=[
            _d("write", True), _d("unknown", None), _d("read", False),
            _d("exempt_one", None), _d("deep_write", True, pinned=False),
        ],
    )
    plain = build_surface(backend, audit=AuditSink(enabled=False))

    async def published(surface):
        async with Client(surface) as c:
            return [t.model_dump(mode="json") for t in await c.list_tools()]

    assert await published(gated) == await published(plain)

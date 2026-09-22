import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from beherouter.errors import AuthError
from beherouter.identity import CallIdentity, IdentityPolicy
from beherouter.models import Backend, ToolDescriptor
from beherouter.surface import build_surface


class SpyExecutor:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append((verb, args, identity))
        return {"result": "ok"}


def _descriptors():
    return [
        ToolDescriptor(
            name="ping",
            verb="ping",
            summary="ping",
            schema={},
            pinned=True,
            mutating=False,
        )
    ]


def _backend(executor, relists=None):
    """A backend whose catalogue is due for a re-list when `relists` is passed.

    ttl_ms=1 means "always stale", so any meta-tool that reaches
    `catalogue.ensure_fresh()` appends to `relists`. That is how a test tells
    "refused the caller" apart from "refused after asking the backend".
    """

    async def relist():
        relists.append(1)
        return _descriptors()

    return Backend(
        name="demo",
        kind="mcp",
        descriptors=_descriptors(),
        executor=executor,
        relist=relist if relists is not None else None,
        ttl_ms=1 if relists is not None else None,
    )


def _policy(result=None, raises=None):
    """A policy whose resolve() does not read a live request."""

    class P(IdentityPolicy):
        def resolve(self):
            if raises is not None:
                raise raises
            return result

        def guard(self):
            if raises is not None:
                raise raises

    return P(surface="demo", mode="bearer", target="header")


async def test_a_surface_without_a_policy_passes_identity_none():
    spy = SpyExecutor()
    surface = build_surface(_backend(spy))
    async with Client(surface) as c:
        await c.call_tool("ping", {})
    assert spy.calls == [("ping", {}, None)]


async def test_a_resolved_identity_reaches_the_executor():
    spy = SpyExecutor()
    ident = CallIdentity(subject="alice", headers={"authorization": "Bearer t"})
    surface = build_surface(_backend(spy), policy=_policy(result=ident))
    async with Client(surface) as c:
        await c.call_tool("ping", {})
    assert spy.calls[0][2] is ident


async def test_run_tool_carries_the_identity_too():
    spy = SpyExecutor()
    ident = CallIdentity(subject="alice", headers={"authorization": "Bearer t"})
    surface = build_surface(_backend(spy), policy=_policy(result=ident))
    async with Client(surface) as c:
        await c.call_tool("run_tool", {"name": "ping", "args": {}})
    assert spy.calls[0][2] is ident


async def test_a_refused_identity_never_reaches_the_backend():
    """Fail closed: no call, not a call with the deployment credential."""
    spy = SpyExecutor()
    surface = build_surface(
        _backend(spy), policy=_policy(raises=AuthError("shared gateway token"))
    )
    async with Client(surface) as c:
        with pytest.raises(ToolError):
            await c.call_tool("ping", {})
    assert spy.calls == []


# --- the read-only meta-tools are gated too ---------------------------------
#
# A surface that refuses a caller's CALLS must also refuse to enumerate itself
# to them: "you do not have access to this surface" is not much of an answer if
# search_tools still lists every tool on it. Each test also asserts the backend
# was never re-listed, so a refused caller cannot drive traffic to a backend
# they may not use.


async def _refused(surface, tool, args):
    async with Client(surface) as c:
        with pytest.raises(ToolError):
            await c.call_tool(tool, args)


async def test_search_tools_refuses_a_caller_the_surface_would_not_serve():
    relists: list[int] = []
    surface = build_surface(
        _backend(SpyExecutor(), relists=relists),
        policy=_policy(raises=AuthError("you do not have access")),
    )
    await asyncio.sleep(0.01)
    await _refused(surface, "search_tools", {"query": "ping"})
    assert relists == []


async def test_describe_tool_refuses_the_same_caller():
    relists: list[int] = []
    surface = build_surface(
        _backend(SpyExecutor(), relists=relists),
        policy=_policy(raises=AuthError("you do not have access")),
    )
    await asyncio.sleep(0.01)
    await _refused(surface, "describe_tool", {"name": "ping"})
    assert relists == []


async def test_context_cost_refuses_the_same_caller():
    relists: list[int] = []
    surface = build_surface(
        _backend(SpyExecutor(), relists=relists),
        policy=_policy(raises=AuthError("you do not have access")),
    )
    await asyncio.sleep(0.01)
    await _refused(surface, "context_cost", {})
    assert relists == []


async def test_run_tool_refuses_before_re_listing_the_catalogue():
    """Refused at the gate, not after a round trip to the backend."""
    spy = SpyExecutor()
    relists: list[int] = []
    surface = build_surface(
        _backend(spy, relists=relists),
        policy=_policy(raises=AuthError("you do not have access")),
    )
    await asyncio.sleep(0.01)
    await _refused(surface, "run_tool", {"name": "ping", "args": {}})
    assert relists == []
    assert spy.calls == []


async def test_a_surface_without_a_policy_leaves_the_meta_tools_open():
    """The no-identity default is unchanged: no gate, no refusal."""
    surface = build_surface(_backend(SpyExecutor()))
    async with Client(surface) as c:
        hits = await c.call_tool("search_tools", {"query": "ping"})
    assert hits.data[0]["name"] == "ping"


async def test_an_authorised_caller_still_searches_a_gated_surface():
    ident = CallIdentity(subject="alice", headers={"authorization": "Bearer t"})
    surface = build_surface(_backend(SpyExecutor()), policy=_policy(result=ident))
    async with Client(surface) as c:
        hits = await c.call_tool("search_tools", {"query": "ping"})
    assert hits.data[0]["name"] == "ping"

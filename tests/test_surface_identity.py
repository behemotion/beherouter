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


def _backend(executor):
    return Backend(
        name="demo",
        kind="mcp",
        descriptors=[
            ToolDescriptor(
                name="ping",
                verb="ping",
                summary="ping",
                schema={},
                pinned=True,
                mutating=False,
            )
        ],
        executor=executor,
    )


def _policy(result=None, raises=None):
    """A policy whose resolve() does not read a live request."""

    class P(IdentityPolicy):
        def resolve(self):
            if raises is not None:
                raise raises
            return result

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

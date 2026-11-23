"""The lean view of a surface (spec 2026-11-22)."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from beherouter.errors import AuthError
from beherouter.identity import IdentityPolicy
from beherouter.models import Backend, ToolDescriptor
from beherouter.surface import build_surface, lean_of


class _Exec:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append((verb, args))
        return {"result": "ok"}


def _backend(ex=None):
    return Backend(
        name="demo", kind="mcp", executor=ex or _Exec(),
        descriptors=[
            ToolDescriptor(name="ping", verb="ping", summary="Ping it.",
                           schema={"type": "object", "properties": {}},
                           pinned=True, mutating=False),
            ToolDescriptor(name="pong", verb="pong", summary="Pong it.",
                           schema={"type": "object", "properties": {}},
                           pinned=False, mutating=True),
        ],
    )


async def _names(mcp):
    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


async def test_lean_lists_only_the_three_meta_tools():
    lean = lean_of(build_surface(_backend()))
    assert await _names(lean) == {"search_tools", "describe_tool", "run_tool"}


async def test_self_discovering_lean_lists_only_the_pins():
    lean = lean_of(build_surface(_backend(), self_discovering=True))
    assert await _names(lean) == {"ping"}


async def test_full_is_unchanged_when_self_discovering():
    full = build_surface(_backend(), self_discovering=True)
    assert await _names(full) == {
        "ping", "search_tools", "describe_tool", "run_tool", "context_cost",
    }


async def test_run_tool_reaches_a_hidden_pin_through_lean():
    ex = _Exec()
    lean = lean_of(build_surface(_backend(ex)))
    async with Client(lean) as c:
        await c.call_tool("run_tool", {"name": "ping", "args": {}})
    assert ex.calls == [("ping", {})]


async def test_direct_call_of_a_hidden_pin_names_run_tool():
    lean = lean_of(build_surface(_backend()))
    async with Client(lean) as c:
        with pytest.raises(ToolError, match=r"run_tool\(name='ping'"):
            await c.call_tool("ping", {})


async def test_a_hidden_pin_the_caller_may_not_see_gets_no_run_tool_hint(monkeypatch):
    import beherouter.identity as identity_mod
    from beherouter.gates import gates_from_entry
    from beherouter.identity import RequestIdentity
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "roles")
    req = RequestIdentity(shared=False, subject="alice",
                          claims={"sub": "alice", "roles": []}, raw_token="t")
    monkeypatch.setattr(identity_mod, "request_identity", lambda wanted=(): req)
    gates = gates_from_entry(RegistryEntry(
        name="demo", plugin="office-mcp",
        authz={"tools": {"ping": {"require_roles": ["admin"]}}}))
    lean = lean_of(build_surface(_backend(), gates=gates))
    async with Client(lean) as c:
        with pytest.raises(ToolError) as err:
            await c.call_tool("ping", {})
    assert "run_tool(name=" not in str(err.value)
    assert "unknown tool 'ping'" in str(err.value)


async def test_direct_call_of_a_truly_unknown_tool_on_lean_is_unknown():
    lean = lean_of(build_surface(_backend()))
    async with Client(lean) as c:
        with pytest.raises(ToolError, match="unknown tool 'nope'"):
            await c.call_tool("nope", {})


async def test_describe_tool_reports_full_pin_status_on_lean():
    lean = lean_of(build_surface(_backend()))
    async with Client(lean) as c:
        out = (await c.call_tool("describe_tool", {"name": "ping"})).data
    assert out["pinned"] is True


async def test_context_cost_reports_both_profiles():
    full = build_surface(_backend())
    async with Client(full) as c:
        out = (await c.call_tool("context_cost", {})).data
    assert set(out["profiles"]) == {"full", "lean"}
    assert out["profiles"]["lean"] < out["profiles"]["full"]


async def test_lean_listing_is_gated_like_full():
    class Refuse(IdentityPolicy):
        def resolve(self):  # pragma: no cover - listing never resolves
            raise AuthError("no")

        def guard(self):
            raise AuthError("no")

    policy = Refuse(surface="demo", require_roles=("admin",))
    assert policy.hides_listing  # else the assertion below proves nothing
    lean = lean_of(build_surface(_backend(), policy=policy))
    assert await _names(lean) == set()

"""Gates built from a registry entry, attached, and run in their fixed order."""

import logging

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import beherouter.identity as identity_mod
from beherouter.audit import AuditSink
from beherouter.gates import (
    ConfirmGate,
    RateLimiter,
    ToolRoleGate,
    gates_from_entry,
    unserved_names,
)
from beherouter.gateway import build_surfaces
from beherouter.identity import RequestIdentity
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.registry import RegistryEntry
from beherouter.surface import build_surface


def _entry(**kw):
    return RegistryEntry(name="bo", plugin="office-mcp", **kw)


def test_no_gate_configured_builds_nothing():
    assert gates_from_entry(_entry()) is None


def test_the_stage_order_is_roles_rate_confirm(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "roles")
    g = gates_from_entry(_entry(
        authz={"tools": {"w": {"require_roles": ["r"]}}, "confirm_mutating": True,
               "confirm_exempt": ["x"], "hide_tools": False},
        rate_limit={"calls": 5, "per_s": 1},
    ))
    assert [type(s) for s in g.stages] == [ToolRoleGate, RateLimiter, ConfirmGate]
    assert g.roles.roles_claim == "roles" and g.hide is False
    assert g.confirm.exempt == frozenset({"x"})


def test_unserved_names_lists_gated_and_exempt_names_the_backend_lacks():
    e = _entry(authz={"tools": {"w": {"require_roles": ["r"]}, "gone": {"require_roles": ["r"]}},
                      "confirm_mutating": True, "confirm_exempt": ["w", "old"]})
    assert unserved_names(e, {"w"}) == ["gone", "old"]


class Spy:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append(verb)
        return {"result": "ok"}


def _caller(monkeypatch, roles):
    req = RequestIdentity(shared=False, subject="alice",
                          claims={"sub": "alice", "roles": roles}, raw_token="t")
    monkeypatch.setattr(identity_mod, "request_identity", lambda wanted=(): req)


def _surface(monkeypatch, **entry_kw):
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "roles")
    spy = Spy()
    d = ToolDescriptor(name="w", verb="w", summary="w", schema={}, pinned=True, mutating=True)
    backend = Backend(name="bo", kind="mcp", executor=spy, descriptors=[d])
    gates = gates_from_entry(_entry(**entry_kw))
    return build_surface(backend, audit=AuditSink(enabled=False), gates=gates), spy


async def test_a_role_refusal_spends_no_token(monkeypatch):
    surface, spy = _surface(
        monkeypatch,
        authz={"tools": {"w": {"require_roles": ["r"]}}},
        rate_limit={"calls": 1, "per_s": 60},
    )
    async with Client(surface) as c:
        _caller(monkeypatch, ["other"])
        refused = await c.call_tool_mcp("w", {})
        _caller(monkeypatch, ["r"])
        allowed = await c.call_tool_mcp("w", {})
    assert refused.meta[META_KEY]["reason"] == "missing_role"
    assert not allowed.isError and spy.calls == ["w"]


async def test_a_rate_limited_call_asks_no_human(monkeypatch):
    asked = []

    async def accept(message, response_type, params, ctx):
        asked.append(message)
        return response_type(value=True)

    surface, _ = _surface(
        monkeypatch, authz={"confirm_mutating": True}, rate_limit={"calls": 1, "per_s": 60}
    )
    async with Client(surface, elicitation_handler=accept) as c:
        await c.call_tool_mcp("w", {})
        second = await c.call_tool_mcp("w", {})
    assert second.meta[META_KEY]["reason"] == "rate_limited" and len(asked) == 1


async def test_a_declined_confirmation_spends_a_token(monkeypatch):
    async def decline(message, response_type, params, ctx):
        return ElicitResult(action="decline")

    surface, _ = _surface(
        monkeypatch, authz={"confirm_mutating": True}, rate_limit={"calls": 1, "per_s": 60}
    )
    async with Client(surface, elicitation_handler=decline) as c:
        first = await c.call_tool_mcp("w", {})
        second = await c.call_tool_mcp("w", {})
    assert first.meta[META_KEY]["reason"] == "confirmation_required"
    assert second.meta[META_KEY]["reason"] == "rate_limited"


async def test_attach_wires_the_gates_and_warns_about_unserved_names(fake_cli_cmd, caplog):
    caplog.set_level(logging.WARNING)
    registry = {
        "faketool": RegistryEntry(
            name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd},
            rate_limit={"calls": 1, "per_s": 60},
            authz={"confirm_mutating": True, "confirm_exempt": ["faketool_search", "nope"]},
        )
    }
    surfaces = await build_surfaces(registry)
    async with Client(surfaces["faketool"]) as c:
        first = await c.call_tool_mcp("faketool_search", {"query": "hello"})
        second = await c.call_tool_mcp("faketool_search", {"query": "hello"})
    assert not first.isError
    assert second.meta[META_KEY]["reason"] == "rate_limited"
    assert any("nope" in r.getMessage() for r in caplog.records)


def test_a_rate_limit_that_is_not_a_table_is_refused():
    import pytest

    from beherouter.errors import UsageError
    from beherouter.gates import validate_rate_limit

    with pytest.raises(UsageError, match="must be a table"):
        validate_rate_limit("bo", [10])


def test_many_long_arguments_are_cut_to_the_overall_cap():
    from beherouter.gates import _SHOWN_ARGS_MAX, _show_args

    shown = _show_args({f"a{i:03}": "x" * 500 for i in range(100)})
    head, body = shown.split("\n", 1)
    assert head.startswith("Arguments (truncated, ")
    assert len(body) == _SHOWN_ARGS_MAX + 1 and body.endswith("…")


async def test_a_client_that_fails_to_ask_is_refused_as_unsupported(monkeypatch):
    import fastmcp.server.dependencies as deps
    import pytest
    from mcp import types
    from mcp.shared.exceptions import McpError

    from beherouter.errors import AuthError

    class _Ctx:
        session = type("S", (), {"check_client_capability": lambda self, wanted: True})()

        async def elicit(self, *a, **k):
            raise McpError(types.ErrorData(code=-32601, message="nope"))

    monkeypatch.setattr(deps, "get_context", lambda: _Ctx())
    d = ToolDescriptor(name="w", verb="w", summary="", schema={}, pinned=True, mutating=True)
    with pytest.raises(AuthError, match="the client failed to ask its user"):
        await ConfirmGate(surface="bo")(None, d, {})

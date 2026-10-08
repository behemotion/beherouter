import json
from types import SimpleNamespace

import pytest

from beherouter import audit
from beherouter.auth import SHARED_CLIENT_ID
from beherouter.errors import UsageError
from beherouter.outcomes import Outcome


def _token(claims, client_id="idp-client"):
    return SimpleNamespace(client_id=client_id, claims=claims, token="raw.jwt.value")


def test_shared_token_caller_has_no_subject():
    assert audit.caller_from_token(_token({}, SHARED_CLIENT_ID), ()) == audit.Caller(auth="shared")


def test_no_token_is_none():
    assert audit.caller_from_token(None, ("email",)) == audit.Caller(auth="none")


def test_only_named_claims_are_recorded():
    claims = {"sub": "u-1", "email": "a@example.com", "roles": ["x"], "name": "Alice"}
    caller = audit.caller_from_token(_token(claims), ("email", "roles"))
    # roles is a list: only scalar claim values are recorded.
    assert caller == audit.Caller(auth="oidc", sub="u-1", claims={"email": "a@example.com"})


def test_claims_env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUDIT_CLAIMS", " email, preferred_username ,sub,")
    assert audit.audit_claims() == ("email", "preferred_username")


@pytest.mark.parametrize(("raw", "on"), [("", True), ("on", True), ("OFF", False)])
def test_enabled_env(monkeypatch, raw, on):
    monkeypatch.setenv("BEHEROUTER_AUDIT", raw)
    assert audit.audit_enabled() is on


def test_enabled_env_refuses_garbage(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUDIT", "maybe")
    with pytest.raises(UsageError, match="BEHEROUTER_AUDIT"):
        audit.audit_enabled()


def test_one_json_line_per_call_without_token_material():
    lines: list[str] = []
    sink = audit.AuditSink(write=lines.append)
    sink.emit(
        call_id="c1", surface="dwh", tool="run_tool", inner_tool="list_tables",
        caller=audit.Caller(auth="oidc", sub="u-1", claims={"email": "a@example.com"}),
        outcome=Outcome("tool_error", "backend_rejected", 400), latency_ms=31012,
    )
    [line] = lines
    body = json.loads(line)
    assert body["event"] == "tool_call"
    assert body["caller"] == {"sub": "u-1", "email": "a@example.com"}
    assert (body["outcome"], body["reason"], body["status"]) == (
        "tool_error", "backend_rejected", 400
    )
    assert body["inner_tool"] == "list_tables" and body["latency_ms"] == 31012
    assert "raw.jwt.value" not in line and "args" not in body


def test_disabled_sink_writes_nothing():
    lines: list[str] = []
    audit.AuditSink(enabled=False, write=lines.append).emit(
        call_id="c", surface="s", tool="t", inner_tool=None,
        caller=audit.Caller(auth="none"), outcome=Outcome("ok"), latency_ms=1,
    )
    assert lines == []


def test_preflight_refuses_a_bad_audit_switch(monkeypatch):
    """Gateway-wide, so a refused boot, never one degraded surface per entry."""
    from beherouter.gateway import preflight

    monkeypatch.setenv("BEHEROUTER_AUDIT", "maybe")
    with pytest.raises(UsageError, match="BEHEROUTER_AUDIT"):
        preflight({})

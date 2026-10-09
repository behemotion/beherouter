"""Enforcement (spec §5.3): a surface-wide stage, before work, meta-tools included."""

import json
import logging

import pytest
from fastmcp import Client

from beherouter.audit import AuditSink, Caller
from beherouter.killswitch import _SWITCHES, stage_for
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.pipeline import CallPipeline
from beherouter.surface import build_surface


@pytest.fixture
def ks_file(tmp_path, monkeypatch):
    p = tmp_path / "ks.json"
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(p))
    _SWITCHES.clear()
    yield p
    _SWITCHES.clear()


class _Exec:
    calls = 0

    async def run(self, verb, args, *, identity=None):
        _Exec.calls += 1
        return {"ok": True}


async def _call(stage, caller=None):
    caller = caller or Caller(auth="shared")
    from beherouter import pipeline as p

    pipe = CallPipeline("dwh", _Exec(), audit=AuditSink(enabled=False), stages=[stage])
    orig = p.current_caller
    p.current_caller = lambda names: caller
    try:
        return await pipe.run("search_tools", lambda scope: _ok())
    finally:
        p.current_caller = orig


async def _ok():
    return {"ok": True}


def test_no_stage_when_unconfigured(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_KILLSWITCH_PATH", raising=False)
    assert stage_for("dwh") is None


async def test_an_empty_switch_lets_the_call_through(ks_file):
    result = await _call(stage_for("dwh"))
    assert result == {"ok": True}


@pytest.mark.parametrize(
    ("data", "scope"),
    [({"all": {}}, "all"), ({"surfaces": {"dwh": {"reason": "INC-42"}}}, "surface")],
)
async def test_a_stopped_surface_refuses_with_scope(ks_file, data, scope):
    ks_file.write_text(json.dumps(data))
    result = await _call(stage_for("dwh"))
    meta = result.meta["io.beherouter/error"]
    assert result.is_error
    assert meta["reason"] == "surface_disabled"
    assert meta["context"] == {"scope": scope}
    assert "INC-42" not in result.content[0].text


async def test_a_blocked_sub_is_refused_without_echoing_it(ks_file, caplog):
    ks_file.write_text(json.dumps({"subjects": {"user-123": {}}}))
    with caplog.at_level(logging.DEBUG):
        result = await _call(stage_for("dwh"), Caller(auth="oidc", sub="user-123"))
    assert result.meta["io.beherouter/error"]["reason"] == "caller_blocked"
    assert "user-123" not in result.content[0].text
    assert "user-123" not in caplog.text.replace('"sub":"user-123"', "")  # audit line only


async def test_a_stopped_surface_wins_over_a_blocked_caller(ks_file):
    ks_file.write_text(json.dumps({"all": {}, "subjects": {"u": {}}}))
    result = await _call(stage_for("dwh"), Caller(auth="oidc", sub="u"))
    assert result.meta["io.beherouter/error"]["reason"] == "surface_disabled"


async def test_a_shared_caller_is_never_matched_as_a_subject(ks_file):
    ks_file.write_text(json.dumps({"subjects": {"<shared>": {}}}))
    result = await _call(stage_for("dwh"))
    assert result == {"ok": True}


def test_the_gauges_follow_the_file(ks_file):
    from beherouter import metrics
    from beherouter.killswitch import configured

    ks_file.write_text(json.dumps({"surfaces": {"dwh": {}}, "subjects": {"a": {}, "b": {}}}))
    metrics.track_killswitch("dwh", configured())
    metrics.track_killswitch("office", configured())
    body = metrics.render()[0].decode()
    assert 'beherouter_surface_disabled{surface="dwh"} 1.0' in body
    assert 'beherouter_surface_disabled{surface="office"} 0.0' in body
    assert "beherouter_blocked_subjects 2.0" in body
    # other tests leave surfaces named "a" in the global registry: assert on the
    # subject series itself -- a bare count, no label carrying a name
    assert [ln for ln in body.splitlines() if ln.startswith("beherouter_blocked_subjects")] == [
        "beherouter_blocked_subjects 2.0"
    ]


async def test_a_stopped_surface_refuses_pinned_and_meta_tools_but_still_lists(ks_file):
    ks_file.write_text(json.dumps({"surfaces": {"dwh": {"reason": "INC-42"}}}))
    _Exec.calls = 0
    d = ToolDescriptor(name="w", verb="w", summary="w", schema={}, pinned=True, mutating=False)
    backend = Backend(name="dwh", kind="mcp", executor=_Exec(), descriptors=[d])
    stage = stage_for("dwh")
    surface = build_surface(
        backend, audit=AuditSink(enabled=False), stages=(stage,) if stage else ()
    )
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
        pinned = await c.call_tool_mcp("w", {})
        meta = await c.call_tool_mcp("search_tools", {"query": "w"})
    assert {"w", "search_tools"} <= names
    for r in (pinned, meta):
        assert r.isError
        assert r.meta[META_KEY]["reason"] == "surface_disabled"
    assert _Exec.calls == 0

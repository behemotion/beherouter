"""Lean instructions are set at attach (spec 2026-11-22)."""

import pytest
from reload_harness import running, write

from beherouter.gateway import build_surfaces
from beherouter.models import Backend, ToolDescriptor
from beherouter.plugins import PLUGINS, register
from beherouter.plugins.spec import PluginSpec
from beherouter.registry import RegistryEntry
from beherouter.surface import lean_of


class _Exec:
    async def run(self, verb, args, *, identity=None):
        return {"result": "ok"}


async def _build(ctx):
    return Backend(name=ctx.surface, kind="mcp", executor=_Exec(), descriptors=[
        ToolDescriptor(name="ping", verb="ping", summary="Ping.", schema={},
                       pinned="ping" in ctx.pinned, mutating=False)])


@pytest.fixture
def plugin():
    names = []

    def make(name, **kw):
        spec = PluginSpec(name=name, summary="t", backing="inproc", pinned=("ping",), **kw)
        register(spec, _build)
        names.append(name)
        return name

    yield make
    for n in names:
        PLUGINS.pop(n, None)


async def test_lean_instructions_carry_recipe_and_entry_text(plugin):
    name = plugin("t-lean", lean_recipe="Use run_tool('ping').")
    entry = RegistryEntry(name="s", plugin=name, instructions="Workspace: homelab.")
    full = (await build_surfaces({"s": entry}))["s"]
    lean = lean_of(full)
    assert "(lean profile)" in lean.instructions
    assert lean.instructions.endswith("Use run_tool('ping').\n\nWorkspace: homelab.")
    assert "homelab" not in full.instructions  # registry text is lean-only


async def test_self_discovering_plugin_gets_pins_on_lean(plugin):
    name = plugin("t-sd", self_discovering=True)
    full = (await build_surfaces({"s": RegistryEntry(name="s", plugin=name)}))["s"]
    assert "own discovery path" in lean_of(full).instructions


async def test_lean_costing_failure_keeps_the_attach(plugin, monkeypatch, caplog):
    import beherouter.costing as costing

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(costing, "lean_instructions", boom)
    name = plugin("t-fail")
    full = (await build_surfaces({"s": RegistryEntry(name="s", plugin=name)}))["s"]
    assert lean_of(full) is not None
    assert full.instructions  # the full listing is intact despite the failure
    assert "lean instructions failed" in caplog.text


async def test_surface_without_a_lean_twin_attaches(plugin, monkeypatch):
    import beherouter.gateway as gateway

    monkeypatch.setattr(gateway, "lean_of", lambda mcp: None)
    name = plugin("t-nolean")
    full = (await build_surfaces({"s": RegistryEntry(name="s", plugin=name)}))["s"]
    assert full.instructions


TOKEN = {"Authorization": "Bearer s3cret", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", "0")


async def _list(c, url, drop_query_after_init=False):
    init = await c.post(url, json=INIT, headers=TOKEN)
    assert init.status_code == 200, init.text
    h = {**TOKEN}
    if "mcp-session-id" in init.headers:
        h["mcp-session-id"] = init.headers["mcp-session-id"]
    later = url.split("?")[0] if drop_query_after_init else url
    await c.post(later, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    return init, await c.post(later, json=LIST, headers=h)


async def test_http_lean_lists_meta_tools_only(tmp_path, plugin, _env):
    name = plugin("t-http", lean_recipe="R")
    reg = tmp_path / "r.toml"
    write(reg, f'[a]\nplugin = "{name}"\n')
    async with running(reg) as (_rt, c):
        init, listed = await _list(c, "/a/mcp?profile=lean")
        assert "(lean profile)" in init.text
        assert '"run_tool"' in listed.text and '"ping"' not in listed.text
        _, full = await _list(c, "/a/mcp")
        assert '"ping"' in full.text and '"context_cost"' in full.text


async def test_http_lean_session_survives_a_dropped_query(tmp_path, plugin, _env):
    name = plugin("t-drop")
    reg = tmp_path / "r.toml"
    write(reg, f'[a]\nplugin = "{name}"\n')
    async with running(reg) as (_rt, c):
        _, listed = await _list(c, "/a/mcp?profile=lean", drop_query_after_init=True)
        assert '"ping"' not in listed.text and '"run_tool"' in listed.text


async def test_http_unknown_profile_is_400(tmp_path, plugin, _env):
    name = plugin("t-400")
    reg = tmp_path / "r.toml"
    write(reg, f'[a]\nplugin = "{name}"\n')
    async with running(reg) as (_rt, c):
        r = await c.post("/a/mcp?profile=tiny", json=INIT, headers=TOKEN)
        assert r.status_code == 400


async def test_stateless_surface_serves_lean(tmp_path, plugin, _env):
    name = plugin("t-sl")
    reg = tmp_path / "r.toml"
    write(reg, f'[a]\nplugin = "{name}"\nstateless = true\n')
    async with running(reg) as (_rt, c):
        _, listed = await _list(c, "/a/mcp?profile=lean")
        assert listed.status_code == 200 and '"ping"' not in listed.text

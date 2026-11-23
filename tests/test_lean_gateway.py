"""Lean instructions are set at attach (spec 2026-11-22)."""

import pytest

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
    assert "lean instructions failed" in caplog.text


async def test_surface_without_a_lean_twin_attaches(plugin, monkeypatch):
    import beherouter.gateway as gateway

    monkeypatch.setattr(gateway, "lean_of", lambda mcp: None)
    name = plugin("t-nolean")
    full = (await build_surfaces({"s": RegistryEntry(name="s", plugin=name)}))["s"]
    assert full.instructions

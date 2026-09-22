"""Out-of-tree plugins: a distribution advertises `beherouter.plugins`.

The plugin protocol was designed for this from the start — `plugins/__init__.py`
has predicted it in a comment since Phase 1 — and until it existed, a team with
a backend of their own had to fork the gateway to attach it. The lookup is the
only thing that changes: `PluginSpec` and `build()` are untouched.
"""

import pytest

from beherouter.errors import UsageError
from beherouter.plugins import PLUGINS, load_entry_point_plugins, register
from beherouter.plugins.spec import PluginSpec


class FakeEntryPoint:
    """The two attributes `load_entry_point_plugins` uses, and nothing else."""

    def __init__(self, name, action):
        self.name = name
        self.value = f"{name}:module"
        self._action = action

    def load(self):
        return self._action()


@pytest.fixture(autouse=True)
def restore_registry():
    """The plugin table is process-global; a test may not leak into the next."""
    before = dict(PLUGINS)
    yield
    PLUGINS.clear()
    PLUGINS.update(before)


def _registers(name):
    spec = PluginSpec(name=name, summary="external", backing="http")

    async def build(ctx):  # pragma: no cover - never attached in these tests
        raise AssertionError("not attached")

    return lambda: register(spec, build)


def test_an_entry_point_plugin_is_registered():
    loaded = load_entry_point_plugins([FakeEntryPoint("acme-crm", _registers("acme-crm"))])
    assert loaded == ["acme-crm"]
    assert PLUGINS["acme-crm"].spec.summary == "external"


def test_a_broken_entry_point_does_not_stop_the_others():
    """⚠️ One third-party package must not cost the gateway every other plugin.

    A registry entry naming the broken one still fails loudly — `get()` reports
    'unknown plugin', at lint, before a deploy — so nothing is served silently
    by a plugin that did not load.
    """

    def explode():
        raise ImportError("no module named 'acme_sdk'")

    loaded = load_entry_point_plugins(
        [FakeEntryPoint("broken", explode), FakeEntryPoint("fine", _registers("fine"))]
    )
    assert loaded == ["fine"]
    assert "broken" not in PLUGINS
    assert "fine" in PLUGINS


def test_an_external_plugin_cannot_shadow_an_in_tree_one():
    """`register` already refuses a duplicate name; this asserts the in-tree
    plugin SURVIVES the attempt rather than being replaced."""
    before = PLUGINS["plane"]
    loaded = load_entry_point_plugins([FakeEntryPoint("plane", _registers("plane"))])
    assert loaded == []
    assert PLUGINS["plane"] is before


def test_a_load_that_registers_nothing_is_not_reported_as_loaded():
    """An entry point pointing at a module that never calls `register` is a
    misconfigured package, not a plugin — saying 'loaded' would be a lie."""
    loaded = load_entry_point_plugins([FakeEntryPoint("empty", lambda: None)])
    assert loaded == []


def test_the_group_name_is_the_documented_one():
    from beherouter.plugins import ENTRY_POINT_GROUP

    assert ENTRY_POINT_GROUP == "beherouter.plugins"


def test_register_still_refuses_a_duplicate_directly():
    spec = PluginSpec(name="plane", summary="x", backing="http")

    async def build(ctx):  # pragma: no cover
        raise AssertionError

    with pytest.raises(UsageError, match="already registered"):
        register(spec, build)

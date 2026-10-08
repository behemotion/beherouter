"""Out-of-tree plugins: a distribution advertises `beherouter.plugins`.

The plugin protocol was designed for this from the start — `plugins/__init__.py`
has predicted it in a comment since Phase 1 — and until it existed, a team with
a backend of their own had to fork the gateway to attach it. The lookup is the
only thing that changes: `PluginSpec` and `build()` are untouched.
"""

import pytest

from beherouter.errors import UsageError
from beherouter.plugins import (
    ENTRY_POINT_FAILURES,
    PLUGINS,
    get,
    load_entry_point_plugins,
    register,
)
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
    failures = list(ENTRY_POINT_FAILURES)
    yield
    PLUGINS.clear()
    PLUGINS.update(before)
    ENTRY_POINT_FAILURES[:] = failures


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


# --- failures are recorded, not only logged ---------------------------------
#
# A log line is invisible to the two places an operator actually looks before a
# deploy: `beherouter plugins` and `registry-lint`. Both now read this record.


def _explode():
    raise ImportError("no module named 'acme_sdk'")


def test_a_failed_entry_point_is_recorded():
    ENTRY_POINT_FAILURES.clear()
    load_entry_point_plugins([FakeEntryPoint("broken", _explode)])
    assert ENTRY_POINT_FAILURES == [
        {
            "entry_point": "broken",
            "value": "broken:module",
            "error": "ImportError: no module named 'acme_sdk'",
        }
    ]


def test_an_entry_point_that_registers_nothing_is_recorded():
    ENTRY_POINT_FAILURES.clear()
    load_entry_point_plugins([FakeEntryPoint("empty", lambda: None)])
    assert [f["entry_point"] for f in ENTRY_POINT_FAILURES] == ["empty"]
    assert "registered nothing" in ENTRY_POINT_FAILURES[0]["error"]


def test_an_unknown_plugin_error_names_the_entry_points_that_failed():
    """The likeliest reason a third-party plugin is 'unknown' is that its
    package failed to import; saying so turns a hunt into a pointer."""
    ENTRY_POINT_FAILURES.clear()
    load_entry_point_plugins([FakeEntryPoint("acme-crm", _explode)])
    with pytest.raises(
        UsageError, match=r"unknown plugin 'acme-crm'.*acme-crm \(acme-crm:module\)"
    ):
        get("acme-crm")


def test_an_unknown_plugin_error_is_unchanged_when_nothing_failed():
    ENTRY_POINT_FAILURES.clear()
    with pytest.raises(UsageError) as e:
        get("nope")
    assert "entry point" not in str(e.value)


def test_the_plugins_verb_lists_failures(capsys, monkeypatch):
    from beherouter.cli import app as cli

    ENTRY_POINT_FAILURES.clear()
    load_entry_point_plugins([FakeEntryPoint("broken", _explode)])
    monkeypatch.setattr(cli.app.ctx, "json", True)
    cli.plugins()
    import json

    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == ENTRY_POINT_FAILURES


def test_registry_lint_warns_about_failed_entry_points(tmp_path, capsys, monkeypatch):
    from beherouter.cli import app as cli

    ENTRY_POINT_FAILURES.clear()
    load_entry_point_plugins([FakeEntryPoint("broken", _explode)])
    reg = tmp_path / "registry.toml"
    reg.write_text('[office]\nplugin = "office-mcp"\n')
    monkeypatch.setattr(cli.app.ctx, "json", True)
    cli.registry_lint(path=str(reg))
    import json

    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert any("broken" in w and "failed to load" in w for w in warnings)

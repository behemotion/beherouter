import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from beherouter.errors import UsageError
from beherouter.plugins import PLUGINS, get, register
from beherouter.plugins.spec import ConfigField, EnvVar, PluginSpec


def _spec(name="demo"):
    return PluginSpec(name=name, summary="a demo", backing="http")


async def _build(ctx):
    return None


def test_register_then_get():
    register(_spec(), _build)
    try:
        assert get("demo").spec.summary == "a demo"
    finally:
        PLUGINS.pop("demo", None)


def test_duplicate_registration_raises():
    register(_spec("dup"), _build)
    with pytest.raises(UsageError, match="dup"):
        register(_spec("dup"), _build)
    PLUGINS.pop("dup")


def test_unknown_plugin_names_the_known_ones():
    with pytest.raises(UsageError, match="office-mcp"):
        get("nope")


def test_spec_is_frozen():
    with pytest.raises(FrozenInstanceError):
        _spec().name = "other"


def test_unknown_backing_is_rejected():
    with pytest.raises(UsageError, match="backing"):
        register(PluginSpec(name="bad", summary="x", backing="carrier-pigeon"), _build)


def test_config_field_and_env_var_are_frozen():
    with pytest.raises(FrozenInstanceError):
        ConfigField(name="a", type=str).name = "b"
    with pytest.raises(FrozenInstanceError):
        EnvVar(name="a").name = "b"


def test_importing_plugins_opens_no_socket():
    """A plugin's DECLARATION must never touch the network. Only build() may.

    Run in a FRESH interpreter rather than reloading in-process: every plugin
    module is already in sys.modules by the time this test runs, so a reload of
    the package would re-execute nothing under the guard while rebinding
    PLUGINS to a new empty dict — silently deregistering every plugin for the
    rest of the session.
    """
    guard = (
        "import socket\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('plugin import opened a socket')\n"
        "socket.socket.connect = boom\n"
        "socket.socket.connect_ex = boom\n"
        "socket.create_connection = boom\n"
        "import beherouter.plugins\n"
    )
    r = subprocess.run([sys.executable, "-c", guard], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_requires_entry_refuses_an_entry_without_the_named_keys():
    from beherouter.errors import UsageError
    from beherouter.plugins import PLUGINS, register
    from beherouter.plugins.spec import PluginSpec
    from beherouter.registry import RegistryEntry, validate_entry

    async def build(ctx):
        raise AssertionError

    register(PluginSpec(name="t-req", summary="t", backing="inproc",
                        requires_entry=("probe", "pinned")), build)
    try:
        with pytest.raises(UsageError, match=r"'s'.*t-req.*probe"):
            validate_entry(RegistryEntry(name="s", plugin="t-req", pinned=["a"]))
        with pytest.raises(UsageError, match=r"'s'.*t-req.*pinned"):
            validate_entry(RegistryEntry(name="s", plugin="t-req", probe="a", pinned=[]))
        validate_entry(RegistryEntry(name="s", plugin="t-req", probe="a", pinned=["a"]))
    finally:
        PLUGINS.pop("t-req", None)

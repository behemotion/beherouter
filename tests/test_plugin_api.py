"""The supported import surface for plugin authors, and its version check."""

import subprocess
import sys
import textwrap

import pytest

from beherouter.errors import UsageError
from beherouter.plugins import PLUGINS, register
from beherouter.plugins.spec import API_VERSION, PluginSpec

EXPORTS = {
    "API_VERSION", "AuthError", "Backend", "CliBacking", "ConfigField", "EnvVar",
    "IdentitySupport", "McpBacking", "PluginContext", "PluginSpec", "ToolDescriptor",
    "Unavailable", "UsageError", "identity_client", "load_cli_backend",
    "load_inproc_backend", "load_mcp_backend", "register",
}


def test_the_facade_exports_exactly_the_documented_names():
    import beherouter.plugin_api as api

    assert set(api.__all__) == EXPORTS
    for name in EXPORTS:
        assert getattr(api, name) is not None, name


def test_the_facade_hands_out_the_real_objects_not_copies():
    import beherouter.plugin_api as api
    from beherouter.plugins import register as real_register

    assert api.register is real_register
    assert api.PluginSpec is PluginSpec


def test_an_unknown_name_is_an_attribute_error():
    import beherouter.plugin_api as api

    with pytest.raises(AttributeError, match="no_such_thing"):
        api.no_such_thing  # noqa: B018


def test_specs_default_to_the_current_api_version():
    assert PluginSpec(name="x", summary="x", backing="cli").api == API_VERSION == 1


def test_register_refuses_a_plugin_built_for_another_api_version():
    spec = PluginSpec(name="t-future", summary="x", backing="cli", api=2)

    async def build(ctx):
        raise AssertionError("never built")

    with pytest.raises(UsageError, match=r"t-future.*v2.*v1"):
        register(spec, build)
    assert "t-future" not in PLUGINS


_EXTERNAL = """
    from beherouter.plugin_api import PluginSpec, register

    async def build(ctx):
        raise NotImplementedError

    register(PluginSpec(name="acme-demo", summary="from outside", backing="cli", api={api}), build)
"""


def _install_fake_distribution(tmp_path, api: int):
    (tmp_path / "acme_demo.py").write_text(textwrap.dedent(_EXTERNAL.format(api=api)))
    info = tmp_path / "acme_demo-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-demo\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[beherouter.plugins]\nacme-demo = acme_demo\n")


def _run(tmp_path, code: str):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_an_external_plugin_registers_when_the_facade_is_the_first_import(tmp_path):
    """The circular-import trap: plugin_api imported FIRST starts entry-point
    loading, and the external module imports plugin_api back."""
    _install_fake_distribution(tmp_path, api=1)
    r = _run(
        tmp_path,
        "from beherouter.plugin_api import register\n"
        "from beherouter.plugins import get\n"
        "print(get('acme-demo').spec.summary)",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "from outside"
    assert "failed to load" not in r.stderr


def test_an_external_plugin_for_another_api_is_skipped_with_a_reason(tmp_path):
    _install_fake_distribution(tmp_path, api=2)
    r = _run(
        tmp_path,
        "from beherouter.plugins import PLUGINS\nprint('acme-demo' in PLUGINS)",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"
    assert "acme-demo" in r.stderr and "v2" in r.stderr


_DECORATOR_PLUGIN = """
    import asyncio
    from fastmcp import FastMCP
    from beherouter.plugin_api import McpBacking, PluginSpec, load_inproc_backend, register

    mcp = FastMCP("acme")

    @mcp.tool
    def lookup_order(order_id: str) -> dict:
        \"\"\"Fetch one order.\"\"\"
        return {"order_id": order_id}

    SPEC = PluginSpec(name="acme-orders", summary="orders", backing="inproc",
                      pinned=("lookup_order",), probe="lookup_order",
                      probe_args={"order_id": "probe"})

    async def build(ctx):
        return await load_inproc_backend(McpBacking(name=ctx.surface, transport="inproc",
                                                    server=mcp, pinned=ctx.pinned))

    register(SPEC, build)
"""


def test_the_decorator_path_attaches_as_an_external_plugin(tmp_path):
    (tmp_path / "acme_orders.py").write_text(textwrap.dedent(_DECORATOR_PLUGIN))
    info = tmp_path / "acme_orders-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-orders\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[beherouter.plugins]\nacme-orders = acme_orders\n")
    r = _run(
        tmp_path,
        "import asyncio\n"
        "from beherouter.gateway import load_backend\n"
        "from beherouter.registry import RegistryEntry\n"
        "b = asyncio.run(load_backend(RegistryEntry(name='orders', plugin='acme-orders')))\n"
        "print(b.kind, [d.name for d in b.pinned])\n"
        "print(asyncio.run(b.executor.run('lookup_order', {'order_id': 'o-1'})))",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "inproc ['lookup_order']",
        "{'result': {'order_id': 'o-1'}}",
    ]

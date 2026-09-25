"""One backend must not take every surface down with it.

AGENTS.md used to carry this as a warning ("an attach failure crash-loops the
whole gateway"). These tests hold the fix: an attach failure is recorded and
isolated; only what `registry-lint` can see still refuses boot.
"""

import asyncio

import pytest

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.errors import Unavailable, UsageError
from beherouter.gateway import build_surfaces
from beherouter.plugins import PLUGINS, register
from beherouter.plugins.spec import PluginContext, PluginSpec
from beherouter.registry import RegistryEntry


@pytest.fixture
def test_plugin():
    """Register a throwaway plugin whose build() the test controls."""
    names: list[str] = []

    def _register(name: str, build) -> None:
        register(PluginSpec(name=name, summary="test", backing="cli"), build)
        names.append(name)

    yield _register
    for n in names:
        PLUGINS.pop(n, None)


def _good(fake_cli_cmd):
    async def build(ctx: PluginContext):
        return load_cli_backend(CliBacking(name=ctx.surface, cmd=fake_cli_cmd))

    return build


async def _boom(ctx: PluginContext):
    raise Unavailable("backend refused the connection")


async def test_a_failing_attach_leaves_the_other_surfaces_up(test_plugin, fake_cli_cmd):
    test_plugin("t-good", _good(fake_cli_cmd))
    test_plugin("t-boom", _boom)
    registry = {
        "bad": RegistryEntry(name="bad", plugin="t-boom"),
        "good": RegistryEntry(name="good", plugin="t-good"),
    }
    failed: dict[str, str] = {}
    surfaces = await build_surfaces(registry, failed=failed)
    assert set(surfaces) == {"good"}
    assert set(failed) == {"bad"}
    assert "refused the connection" in failed["bad"]


async def test_without_a_failed_dict_an_attach_failure_still_raises(test_plugin):
    """Existing callers (CLI context-cost, health) keep today's behaviour."""
    test_plugin("t-boom", _boom)
    with pytest.raises(Unavailable):
        await build_surfaces({"bad": RegistryEntry(name="bad", plugin="t-boom")})


async def test_a_hung_attach_times_out_into_failed(test_plugin, monkeypatch):
    async def hang(ctx):
        await asyncio.sleep(30)

    test_plugin("t-hang", hang)
    monkeypatch.setenv("BEHEROUTER_ATTACH_TIMEOUT_S", "0.05")
    failed: dict[str, str] = {}
    surfaces = await asyncio.wait_for(
        build_surfaces({"slow": RegistryEntry(name="slow", plugin="t-hang")}, failed=failed),
        timeout=5,
    )
    assert surfaces == {}
    assert "timed out" in failed["slow"]


async def test_a_registry_error_still_refuses_boot_even_when_isolating():
    """What registry-lint can see is an operator mistake with a local fix;
    booting around it would hide it."""
    registry = {"x": RegistryEntry(name="x", plugin="no-such-plugin")}
    with pytest.raises(UsageError, match="unknown plugin"):
        await build_surfaces(registry, failed={})


async def test_an_unset_secret_still_refuses_boot_even_when_isolating(
    test_plugin, fake_cli_cmd, monkeypatch
):
    from beherouter.plugins.spec import EnvVar

    async def build(ctx):
        return load_cli_backend(CliBacking(name=ctx.surface, cmd=fake_cli_cmd))

    register(
        PluginSpec(name="t-secret", summary="t", backing="cli", env=(EnvVar("token"),)),
        build,
    )
    try:
        monkeypatch.delenv("T_SECRET_UNSET", raising=False)
        entry = RegistryEntry(
            name="s", plugin="t-secret", env={"token": "${T_SECRET_UNSET}"}
        )
        with pytest.raises(UsageError, match="T_SECRET_UNSET"):
            await build_surfaces({"s": entry}, failed={})
    finally:
        PLUGINS.pop("t-secret", None)


@pytest.mark.parametrize("raw", ["0", "-1", "soon"])
def test_a_bad_attach_timeout_is_refused(raw, monkeypatch):
    from beherouter.gateway import attach_timeout_s

    monkeypatch.setenv("BEHEROUTER_ATTACH_TIMEOUT_S", raw)
    with pytest.raises(UsageError, match="BEHEROUTER_ATTACH_TIMEOUT_S"):
        attach_timeout_s()


def test_the_attach_timeout_defaults_to_30s(monkeypatch):
    from beherouter.gateway import attach_timeout_s

    monkeypatch.delenv("BEHEROUTER_ATTACH_TIMEOUT_S", raising=False)
    assert attach_timeout_s() == 30.0

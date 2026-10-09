"""Triggers (spec §2.2)."""

import asyncio
import os
import signal

import pytest
from reload_harness import running, write

from beherouter.errors import UsageError
from beherouter.gateway import build_gateway_app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", "0")


async def _until(pred, n=300):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_sighup_reloads(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        # without the handler SIGHUP's default action would kill the test process
        assert rt._sighup
        write(reg, '[a]\nplugin = "t-ok"\n')
        os.kill(os.getpid(), signal.SIGHUP)
        await _until(lambda: "a" in rt.table)


async def test_on_sighup_reloads(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        write(reg, '[a]\nplugin = "t-ok"\n')
        rt.on_sighup()
        await _until(lambda: "a" in rt.table)


async def test_the_watch_reloads_a_changed_registry(
    tmp_path, gateway_plugin, exec_builder, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "0.02")
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        assert rt._watch_task is not None
        write(reg, '[a]\nplugin = "t-ok"\n')
        await _until(lambda: "a" in rt.table)


async def test_the_watch_is_off_by_default(tmp_path):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        assert rt._watch_task is None


async def test_the_watch_sees_a_rotated_file_secret(tmp_path, monkeypatch):
    from beherouter.models import Backend
    from beherouter.plugins import PLUGINS, register
    from beherouter.plugins.spec import EnvVar, PluginSpec

    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "0.02")
    seen: list[str] = []

    class _E:
        async def run(self, verb, args, *, identity=None):
            return {}

        async def aclose(self):
            pass

    async def build(ctx):
        seen.append(ctx.env["api_key"])
        return Backend(name=ctx.surface, kind="mcp", descriptors=[], executor=_E())

    register(
        PluginSpec(name="t-key", summary="t", backing="inproc", env=(EnvVar("api_key"),)),
        build,
    )
    try:
        key = tmp_path / "key"
        key.write_text("one\n")
        reg = tmp_path / "r.toml"
        write(reg, f'[a]\nplugin = "t-key"\n[a.env]\napi_key = "${{file:{key}}}"\n')
        async with running(reg) as (_rt, _c):
            write(key, "two\n")
            await _until(lambda: seen == ["one", "two"])
    finally:
        PLUGINS.pop("t-key", None)


def _rotating_plugin(key, seen: list[str]):
    """A plugin whose FIRST build sees the secret, then the secret is rotated
    while that attach is still running (M1: the boot fingerprint race)."""
    from beherouter.models import Backend
    from beherouter.plugins import register
    from beherouter.plugins.spec import EnvVar, PluginSpec

    class _E:
        async def run(self, verb, args, *, identity=None):
            return {}

        async def aclose(self):
            pass

    async def build(ctx):
        seen.append(ctx.env["api_key"])
        if len(seen) == 1:
            write(key, "two\n")
        return Backend(name=ctx.surface, kind="mcp", descriptors=[], executor=_E())

    register(
        PluginSpec(name="t-rot", summary="t", backing="inproc", env=(EnvVar("api_key"),)),
        build,
    )


@pytest.mark.parametrize("trigger", ["watch", "admin"])
async def test_a_secret_rotated_during_boot_attach_is_reattached(
    tmp_path, monkeypatch, trigger
):
    from beherouter.plugins import PLUGINS

    if trigger == "watch":
        monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "0.02")
    seen: list[str] = []
    key = tmp_path / "key"
    key.write_text("one\n")
    _rotating_plugin(key, seen)
    try:
        reg = tmp_path / "r.toml"
        write(reg, f'[a]\nplugin = "t-rot"\n[a.env]\napi_key = "${{file:{key}}}"\n')
        async with running(reg) as (rt, _c):
            if trigger == "admin":
                result = await rt.reloader.request("admin")
                assert result["changed"] == ["a"]
            await _until(lambda: seen == ["one", "two"])
    finally:
        PLUGINS.pop("t-rot", None)


async def test_a_failing_reload_does_not_stop_the_watch(
    tmp_path, gateway_plugin, exec_builder, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "0.02")
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        write(reg, "this is not toml [")
        await _until(lambda: rt.last_reload_failure is not None)
        write(reg, '[a]\nplugin = "t-ok"\n')
        await _until(lambda: "a" in rt.table)


async def test_shutdown_removes_the_signal_handler_and_the_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "0.02")
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, _c):
        watch = rt._watch_task
    assert watch is not None and watch.done()
    assert not rt._sighup
    loop = asyncio.get_running_loop()
    assert not loop.remove_signal_handler(signal.SIGHUP)


@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "soon"])
async def test_a_bad_watch_interval_refuses_boot(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", bad)
    reg = tmp_path / "r.toml"
    write(reg, "")
    with pytest.raises(UsageError, match="BEHEROUTER_REGISTRY_WATCH_S"):
        await build_gateway_app(reg)

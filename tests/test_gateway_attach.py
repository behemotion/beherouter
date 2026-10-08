"""Attach behaviour beyond isolation: concurrency, non-transient faults, pin drift.

`test_gateway_isolation.py` holds "one dead backend must not take the rest
down". This file holds what attach does with the time and the information it
has: surfaces attach concurrently (boot costs the slowest backend, not the sum),
a configuration fault is not retried forever, and a pin the backend does not
serve is said out loud at attach rather than found later by `health --deep`.
"""

import asyncio
import logging
import time

import httpx
import pytest

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.errors import Unavailable, UsageError
from beherouter.gateway import build_gateway_app, build_surfaces, missing_pins
from beherouter.health import deep_health
from beherouter.models import Backend, ToolDescriptor
from beherouter.plugins import PLUGINS, register, resolve_probe
from beherouter.plugins.spec import PluginContext, PluginSpec
from beherouter.registry import RegistryEntry

DELAY = 0.3


@pytest.fixture
def test_plugin():
    """Register throwaway plugins whose build() the test controls."""
    names: list[str] = []

    def _register(name: str, build, **spec) -> None:
        register(PluginSpec(name=name, summary="test", backing="cli", **spec), build)
        names.append(name)

    yield _register
    for n in names:
        PLUGINS.pop(n, None)


def _slow_good(fake_cli_cmd, delay=DELAY):
    async def build(ctx: PluginContext):
        await asyncio.sleep(delay)
        return load_cli_backend(CliBacking(name=ctx.surface, cmd=fake_cli_cmd))

    return build


def _slow_boom(delay, exc):
    async def build(ctx: PluginContext):
        await asyncio.sleep(delay)
        raise exc

    return build


# --- 1. concurrency -----------------------------------------------------------


async def test_surfaces_attach_concurrently_in_registry_order(test_plugin, fake_cli_cmd):
    """Three backends each taking DELAY must cost ~DELAY at boot, not 3×DELAY —
    a serial attach multiplies the slowest backend's latency by the registry
    size, and with a 30 s timeout per surface that is minutes of dead gateway."""
    test_plugin("t-slow", _slow_good(fake_cli_cmd))
    registry = {
        n: RegistryEntry(name=n, plugin="t-slow") for n in ("zeta", "alpha", "mid")
    }
    start = time.perf_counter()
    surfaces = await build_surfaces(registry, failed={})
    elapsed = time.perf_counter() - start
    assert list(surfaces) == ["zeta", "alpha", "mid"]  # registry order, not finish order
    assert elapsed < 2 * DELAY, f"attach looks serial: {elapsed:.2f}s"


async def test_without_failed_the_first_failure_in_registry_order_raises(test_plugin):
    """Deterministic: the error raised is the first in REGISTRY order, not the
    first to finish — otherwise the same broken registry reports a different
    fault from one boot to the next."""
    test_plugin("t-late", _slow_boom(DELAY, Unavailable("first in order")))
    test_plugin("t-early", _slow_boom(0.0, Unavailable("finishes first")))
    registry = {
        "a": RegistryEntry(name="a", plugin="t-late"),
        "b": RegistryEntry(name="b", plugin="t-early"),
    }
    with pytest.raises(Unavailable, match="first in order"):
        await build_surfaces(registry)


async def test_concurrent_attach_still_isolates_and_times_out_per_surface(
    test_plugin, fake_cli_cmd, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_ATTACH_TIMEOUT_S", str(DELAY * 2))
    test_plugin("t-slow", _slow_good(fake_cli_cmd))
    test_plugin("t-hang", _slow_boom(30, Unavailable("never")))
    registry = {
        "hang": RegistryEntry(name="hang", plugin="t-hang"),
        "ok": RegistryEntry(name="ok", plugin="t-slow"),
    }
    failed: dict[str, str] = {}
    surfaces = await asyncio.wait_for(build_surfaces(registry, failed=failed), 5)
    assert list(surfaces) == ["ok"]
    assert "timed out" in failed["hang"]


async def test_deep_health_checks_concurrently_in_registry_order():
    class _Ok:
        async def run(self, verb, args, *, identity=None):
            return {"result": "ok"}

    async def _load(entry):
        await asyncio.sleep(DELAY)
        return Backend(name=entry.name, kind="mcp", descriptors=[], executor=_Ok())

    registry = {
        n: RegistryEntry(name=n, plugin="none", pinned=[]) for n in ("c", "a", "b")
    }
    start = time.perf_counter()
    records = await deep_health(registry, load=_load)
    elapsed = time.perf_counter() - start
    assert [r["name"] for r in records] == ["c", "a", "b"]
    assert elapsed < 2 * DELAY, f"deep health looks serial: {elapsed:.2f}s"


# --- 2. a configuration fault is not retried -----------------------------------


async def _healthz(c) -> dict:
    return (await c.get("/healthz")).json()


async def test_a_usage_error_at_attach_is_not_retried(test_plugin, monkeypatch, caplog):
    """A UsageError is a configuration fault: retrying it forever only fills
    the log. The surface stays 503, `/healthz` says it needs a config change,
    and the build is attempted exactly once."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    calls = {"n": 0}

    async def misconfigured(ctx):
        calls["n"] += 1
        raise UsageError("bad option")

    test_plugin("t-usage", misconfigured)
    app = await build_gateway_app(
        {"bad": RegistryEntry(name="bad", plugin="t-usage")},
        retry_initial_s=0.01,
        retry_max_s=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.ERROR, logger="beherouter.gateway"):
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            await asyncio.sleep(0.1)
            health = await _healthz(c)
            r = await c.post("/bad/mcp", json={})
    assert calls["n"] == 1
    assert health == {
        "status": "degraded",
        "surfaces": [],
        "failed": ["bad"],
        "needs_config_change": ["bad"],
    }
    assert r.status_code == 503
    assert "not be retried" in r.json()["detail"]
    assert "bad option" not in r.text  # never echo the error before auth
    assert "Retry-After" not in r.headers
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 1 and "configuration" in errors[0].getMessage()


async def test_a_usage_error_on_a_retry_stops_the_retry_loop(test_plugin, monkeypatch):
    """Transient at boot, then a config fault (e.g. the backend's catalogue
    now trips an identity refusal): the loop stops instead of spinning."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    calls = {"n": 0}

    async def flaky_then_bad(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable("not yet")
        raise UsageError("bad option")

    test_plugin("t-turns-bad", flaky_then_bad)
    app = await build_gateway_app(
        {"bad": RegistryEntry(name="bad", plugin="t-turns-bad")},
        retry_initial_s=0.01,
        retry_max_s=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        await asyncio.sleep(0.2)
        health = await _healthz(c)
    assert calls["n"] == 2
    assert health["needs_config_change"] == ["bad"]


async def test_a_transient_failure_is_not_marked_as_a_config_fault(
    test_plugin, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    test_plugin("t-down", _slow_boom(0, Unavailable("down")))
    app = await build_gateway_app(
        {"x": RegistryEntry(name="x", plugin="t-down")}, retry_initial_s=3600
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        health = await _healthz(c)
        r = await c.post("/x/mcp", json={})
    assert "needs_config_change" not in health
    assert r.headers["Retry-After"] == "30"


# --- 3. pins the backend does not serve ------------------------------------------


def _desc(name, verb=None):
    return ToolDescriptor(
        name=name, verb=verb or name, summary="s", schema={}, pinned=True, mutating=None
    )


def test_missing_pins_compares_names_for_mcp_and_verbs_for_cli():
    mcp = Backend(name="m", kind="mcp", descriptors=[_desc("alive")], executor=None)
    cli = Backend(
        name="c", kind="cli", descriptors=[_desc("c_search", "search")], executor=None
    )
    assert missing_pins(mcp, ["alive", "gone"]) == ["gone"]
    assert missing_pins(cli, ["search"]) == []
    assert missing_pins(cli, ["c_search"]) == ["c_search"]


async def test_an_unserved_pin_warns_and_shows_on_healthz_without_failing(
    test_plugin, fake_cli_cmd, monkeypatch, caplog
):
    """Attach does not fail on it — a backend that lost one pinned tool still
    serves every other one — but it is no longer silent: a WARNING at attach
    and a `pinned_missing` map on /healthz. `status` stays `ok`: it means "every
    surface attached", and an external probe greps it; `health --deep` is the
    check that turns this into a failure."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    test_plugin("t-good", _slow_good(fake_cli_cmd, 0))
    entry = RegistryEntry(name="ft", plugin="t-good", pinned=["search", "vanished"])
    with caplog.at_level(logging.WARNING, logger="beherouter.gateway"):
        app = await build_gateway_app({"ft": entry})
    assert any(
        "vanished" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        health = await _healthz(c)
    assert health == {
        "status": "ok",
        "surfaces": ["ft"],
        "pinned_missing": {"ft": ["vanished"]},
    }


async def test_build_surfaces_reports_missing_pins_into_the_given_map(
    test_plugin, fake_cli_cmd
):
    test_plugin("t-good", _slow_good(fake_cli_cmd, 0))
    registry = {
        "ok": RegistryEntry(name="ok", plugin="t-good", pinned=["search"]),
        "drift": RegistryEntry(name="drift", plugin="t-good", pinned=["gone"]),
    }
    missing: dict[str, list[str]] = {}
    await build_surfaces(registry, failed={}, pinned_missing=missing)
    assert missing == {"drift": ["gone"]}


# --- 4. one probe resolver ------------------------------------------------------


def test_resolve_probe_treats_probe_and_args_as_one_unit(test_plugin):
    async def build(ctx):
        raise AssertionError("never built")

    test_plugin("t-probe", build, probe="default_tool", probe_args={"q": "x"})
    plugin = PLUGINS["t-probe"]
    # no override -> the plugin's tested pair
    assert resolve_probe(RegistryEntry(name="s", plugin="t-probe"), plugin) == (
        "default_tool",
        {"q": "x"},
    )
    # an overriding probe never inherits the plugin's arguments
    assert resolve_probe(
        RegistryEntry(name="s", plugin="t-probe", probe="get_me"), plugin
    ) == ("get_me", None)
    # an unknown plugin (health tolerates one) -> no probe at all
    assert resolve_probe(RegistryEntry(name="s", plugin="nope"), None) == (None, None)

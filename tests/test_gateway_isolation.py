"""One backend must not take every surface down with it.

AGENTS.md used to carry this as a warning ("an attach failure crash-loops the
whole gateway"). These tests hold the fix: an attach failure is recorded and
isolated; only what `registry-lint` can see still refuses boot.
"""

import asyncio

import httpx
import pytest

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.errors import Unavailable, UsageError
from beherouter.gateway import build_gateway_app, build_surfaces
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


MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def _healthz(c) -> dict:
    return (await c.get("/healthz")).json()


async def _list_tools(c, path: str, token: str) -> httpx.Response:
    """`tools/list` after the streamable-HTTP handshake it requires: a bare
    list without a session is a 400, which would hide what is being tested."""
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    init = await c.post(path, json=INIT, headers=headers)
    assert init.status_code == 200, init.text
    headers["mcp-session-id"] = init.headers["mcp-session-id"]
    await c.post(
        path,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    return await c.post(path, json=LIST, headers=headers)


async def test_a_failed_surface_answers_503_problem_json_without_the_error(
    test_plugin, fake_cli_cmd, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    test_plugin("t-good", _good(fake_cli_cmd))
    test_plugin("t-boom", _boom)
    registry = {
        "bad": RegistryEntry(name="bad", plugin="t-boom"),
        "good": RegistryEntry(name="good", plugin="t-good"),
    }
    app = await build_gateway_app(registry, retry_initial_s=3600)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        r = await c.post("/bad/mcp", json=LIST, headers=MCP_HEADERS)
        assert r.status_code == 503
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert body["status"] == 503 and "bad" in body["detail"]
        # never echo the attach error to an unauthenticated caller
        assert "refused the connection" not in r.text

        health = await _healthz(c)
        assert health == {"status": "degraded", "surfaces": ["good"], "failed": ["bad"]}

        ok = await _list_tools(c, "/good/mcp", "s3cret")
        assert ok.status_code == 200


async def test_healthz_payload_is_unchanged_when_nothing_failed(
    test_plugin, fake_cli_cmd, monkeypatch
):
    """The helm test and the homelab probe grep this exact shape."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    test_plugin("t-good", _good(fake_cli_cmd))
    app = await build_gateway_app({"good": RegistryEntry(name="good", plugin="t-good")})
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        assert await _healthz(c) == {"status": "ok", "surfaces": ["good"]}


async def test_a_failed_surface_is_swapped_in_once_a_retry_succeeds(
    test_plugin, fake_cli_cmd, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    calls = {"n": 0}
    good = _good(fake_cli_cmd)

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable("not yet")
        return await good(ctx)

    test_plugin("t-flaky", flaky)
    registry = {"late": RegistryEntry(name="late", plugin="t-flaky")}
    app = await build_gateway_app(registry, retry_initial_s=0.01, retry_max_s=0.05)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        for _ in range(200):
            if (await _healthz(c))["status"] == "ok":
                break
            await asyncio.sleep(0.02)
        assert await _healthz(c) == {"status": "ok", "surfaces": ["late"]}
        r = await _list_tools(c, "/late/mcp", "s3cret")
        assert r.status_code == 200
        assert "late_search" in r.text  # the fake CLI's pinned verb, published after retry
    assert calls["n"] == 2  # retried exactly until it worked, then stopped


async def test_shutdown_cancels_pending_retries(test_plugin, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    test_plugin("t-boom", _boom)
    app = await build_gateway_app(
        {"bad": RegistryEntry(name="bad", plugin="t-boom")},
        retry_initial_s=0.01,
        retry_max_s=0.01,
    )
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    # leaving the lifespan must not hang or leak a running retry task
    pending = [t for t in asyncio.all_tasks() if "retry" in (t.get_name() or "")]
    assert all(t.done() for t in pending)

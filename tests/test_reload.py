"""Reload (spec §2.3-§2.5): only what changed is re-attached."""

import asyncio
import contextlib

import pytest
from reload_harness import running, write

from beherouter import metrics, runtime
from beherouter.errors import Unavailable, UsageError
from beherouter.gateway import build_gateway_app

TOKEN = {"Authorization": "Bearer s3cret"}
MCP = {**TOKEN, "Accept": "application/json, text/event-stream"}
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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", "0")


async def _until(predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def test_added_surface_goes_live(tmp_path, gateway_plugin, exec_builder):
    execs = []
    gateway_plugin("t-ok", exec_builder(execs))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "ok"
        assert (result["added"], result["unchanged"]) == (["b"], ["a"])
        assert (await c.get("/healthz")).json()["surfaces"] == ["a", "b"]
    assert len(execs) == 2  # 'a' was not rebuilt


async def test_unchanged_surface_keeps_its_app(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, _c):
        app_a = rt.table.get("a").app
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\ncall_timeout_s = 5\n')
        result = await rt.reloader.request("admin")
        assert result["changed"] == ["b"]
        assert rt.table.get("a").app is app_a


async def test_a_session_on_an_unchanged_surface_survives_a_sibling_reload(
    tmp_path, gateway_plugin, exec_builder
):
    """Spec §8: one MCP session stays open across a reload that changes a sibling."""
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        init = await c.post("/a/mcp", json=INIT, headers=MCP)
        assert init.status_code == 200, init.text
        headers = {**MCP, "mcp-session-id": init.headers["mcp-session-id"]}
        await c.post(
            "/a/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\ncall_timeout_s = 5\n')
        assert (await rt.reloader.request("admin"))["changed"] == ["b"]
        listed = await c.post(
            "/a/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        assert '"tools"' in listed.text


async def test_changed_surface_is_swapped_and_the_old_backend_closed(
    tmp_path, gateway_plugin, exec_builder
):
    execs = []
    gateway_plugin("t-ok", exec_builder(execs))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, _c):
        old = rt.table.get("a").app
        write(reg, '[a]\nplugin = "t-ok"\ncall_timeout_s = 5\n')
        await rt.reloader.request("admin")
        assert rt.table.get("a").app is not old
        await _until(lambda: execs[0].closed)
        assert execs[0].closed == 1 and execs[1].closed == 0


async def test_removed_surface_404s_and_loses_its_gauges(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[gone]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        up = ("beherouter_surface_up", {"surface": "gone"})
        assert metrics.REGISTRY.get_sample_value(*up) == 1.0
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["removed"] == ["gone"]
        assert (await c.post("/gone/mcp", headers=TOKEN, json={})).status_code == 404
        # the gauges go (spec §6.1); a counter would stay
        assert metrics.REGISTRY.get_sample_value(*up) is None
        assert 'beherouter_surface_up{surface="gone"}' not in metrics.render()[0].decode()


async def test_a_removed_surface_closes_its_backend_after_the_drain(
    tmp_path, gateway_plugin, exec_builder
):
    by_surface: dict = {}
    inner = exec_builder([])

    async def build(ctx):
        backend = await inner(ctx)
        by_surface[ctx.surface] = backend.executor
        return backend

    gateway_plugin("t-ok", build)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[gone]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, _c):
        write(reg, '[a]\nplugin = "t-ok"\n')
        await rt.reloader.request("admin")
        await _until(lambda: by_surface["gone"].closed)
        assert by_surface["gone"].closed == 1
        assert by_surface["a"].closed == 0


async def test_lint_failure_keeps_the_old_registry(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "no-such-plugin"\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "failed" and "unknown plugin" in result["error"]
        body = (await c.get("/healthz")).json()
        assert body["surfaces"] == ["a"] and body["last_reload"]["status"] == "failed"
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["outcome"] == "ok"
        assert "last_reload" not in (await c.get("/healthz")).json()


async def test_changed_surface_whose_attach_fails_keeps_serving(
    tmp_path, gateway_plugin, exec_builder
):
    calls = {"n": 0}
    good = exec_builder([])

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 2:  # the reload's attach
            raise Unavailable("down")
        return await good(ctx)

    gateway_plugin("t-flaky", flaky)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-flaky"\n')
    async with running(reg) as (rt, c):
        old = rt.table.get("a").app
        write(reg, '[a]\nplugin = "t-flaky"\ncall_timeout_s = 5\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "partial" and result["failed"] == ["a"]
        body = (await c.get("/healthz")).json()
        assert body["status"] == "ok" and body["reload_failed"] == ["a"]
        assert rt.table.get("a").app is old
        await _until(lambda: rt.table.get("a").app is not old)  # the retry swaps it in
        assert rt.table.get("a").app is not old
        assert "reload_failed" not in (await c.get("/healthz")).json()


async def test_added_surface_whose_attach_fails_is_pending_then_retried(
    tmp_path, gateway_plugin, exec_builder
):
    calls = {"n": 0}
    good = exec_builder([])

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:  # the reload's attach
            raise Unavailable("down")
        return await good(ctx)

    gateway_plugin("t-ok", exec_builder([]))
    gateway_plugin("t-flaky", flaky)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-flaky"\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "partial" and result["failed"] == ["b"]
        assert (await c.get("/healthz")).json() == {
            "status": "degraded", "surfaces": ["a"], "failed": ["b"]
        }
        assert (await c.post("/b/mcp", headers=TOKEN, json={})).status_code == 503
        await _until(lambda: rt.table.get("b").app is not None)
        assert (await c.get("/healthz")).json() == {"status": "ok", "surfaces": ["a", "b"]}


async def test_added_surface_with_a_config_fault_is_not_retried(
    tmp_path, gateway_plugin, exec_builder
):
    async def broken(ctx):
        raise UsageError("bad config")

    gateway_plugin("t-ok", exec_builder([]))
    gateway_plugin("t-broken", broken)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-broken"\n')
        assert (await rt.reloader.request("admin"))["failed"] == ["b"]
        body = (await c.get("/healthz")).json()
        assert body["failed"] == ["b"] and body["needs_config_change"] == ["b"]
        slot = rt.table.get("b")
        assert slot.retry is None and slot.config_fault


async def test_changed_surface_with_a_config_fault_keeps_serving_unretried(
    tmp_path, gateway_plugin, exec_builder
):
    calls = {"n": 0}
    good = exec_builder([])

    async def second_is_broken(ctx):
        calls["n"] += 1
        if calls["n"] == 2:
            raise UsageError("bad config")
        return await good(ctx)

    gateway_plugin("t-x", second_is_broken)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-x"\n')
    async with running(reg) as (rt, c):
        old = rt.table.get("a").app
        write(reg, '[a]\nplugin = "t-x"\ncall_timeout_s = 5\n')
        assert (await rt.reloader.request("admin"))["outcome"] == "partial"
        assert rt.table.get("a").app is old and rt.table.get("a").retry is None
        body = (await c.get("/healthz")).json()
        assert body == {"status": "ok", "surfaces": ["a"], "reload_failed": ["a"]}


async def test_changed_pending_surface_drops_its_old_retry(
    tmp_path, gateway_plugin, exec_builder
):
    async def down(ctx):
        raise Unavailable("down")

    gateway_plugin("t-down", down)
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-down"\n')
    async with running(reg) as (rt, c):
        old_retry = rt.table.get("a").retry
        assert old_retry is not None
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["outcome"] == "ok"
        assert old_retry.cancelled()
        assert (await c.get("/healthz")).json() == {"status": "ok", "surfaces": ["a"]}


async def test_reload_counts_by_trigger_and_outcome(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    before = metrics.REGISTRY.get_sample_value(
        "beherouter_reloads_total", {"trigger": "admin", "outcome": "ok"}
    ) or 0
    async with running(reg) as (rt, _c):
        await rt.reloader.request("admin")
    after = metrics.REGISTRY.get_sample_value(
        "beherouter_reloads_total", {"trigger": "admin", "outcome": "ok"}
    )
    assert after == before + 1


async def test_rate_limiter_survives_a_reload_when_its_limit_is_equal(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    rl = "[a.rate_limit]\ncalls = 1\nper_s = 60\n"
    write(reg, '[a]\nplugin = "t-ok"\n' + rl)
    async with running(reg) as (rt, _c):
        limiter = rt.limiters["a"]
        write(reg, '[a]\nplugin = "t-ok"\ncall_timeout_s = 5\n' + rl)
        await rt.reloader.request("admin")
        assert rt.limiters["a"] is limiter


async def test_a_changed_rate_limit_gets_a_fresh_limiter(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[a.rate_limit]\ncalls = 1\nper_s = 60\n')
    async with running(reg) as (rt, _c):
        limiter = rt.limiters["a"]
        write(reg, '[a]\nplugin = "t-ok"\n[a.rate_limit]\ncalls = 2\nper_s = 60\n')
        assert (await rt.reloader.request("admin"))["changed"] == ["a"]
        assert rt.limiters["a"] is not limiter
        assert rt.limiters["a"].calls == 2


async def test_in_memory_registry_cannot_reload(gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    app = await build_gateway_app({})
    async with app.router.lifespan_context(app):
        result = await app.state.runtime.reloader.request("admin")
    assert result["outcome"] == "failed" and "in-memory" in result["error"]


async def test_shutdown_cancels_an_in_flight_reload(tmp_path, gateway_plugin, exec_builder):
    """No reload task outlives the lifespan."""
    entered = asyncio.Event()
    good = exec_builder([])

    async def hang(ctx):
        if ctx.surface == "b":
            entered.set()
            await asyncio.Event().wait()
        return await good(ctx)

    gateway_plugin("t-hang", hang)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-hang"\n')
    async with running(reg) as (rt, _c):
        write(reg, '[a]\nplugin = "t-hang"\n[b]\nplugin = "t-hang"\n')
        requester = asyncio.create_task(rt.reloader.request("admin"))
        await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(RuntimeError, match="cancelled"):
        await asyncio.wait_for(requester, 1)
    assert rt.reloader.idle


def _held_supervisors(monkeypatch, hold: dict):
    """Patch the runtime's Supervisor so the app of a surface named in `hold`
    blocks inside its REAL lifespan entry (`hold[name]` = (entered, gate)), or
    fails it (`hold[name]` = an exception). Each entry applies once."""

    class Held(runtime.Supervisor):
        async def start(self):
            how = hold.pop(self.name, None)
            if how is not None:
                orig = self.surface.http_app

                def http_app(path):
                    app = orig(path=path)
                    inner = app.router.lifespan_context

                    @contextlib.asynccontextmanager
                    async def held(a):
                        if isinstance(how, Exception):
                            raise how
                        entered, gate = how
                        entered.set()
                        await gate.wait()
                        async with inner(a):
                            yield

                    app.router.lifespan_context = held
                    return app

                self.surface.http_app = http_app
            return await super().start()

    monkeypatch.setattr(runtime, "Supervisor", Held)


def _first_fails_then(execs: list, exec_builder):
    calls = {"n": 0}
    good = exec_builder(execs)

    async def build(ctx):
        calls["n"] += 1
        if calls["n"] == 1:  # boot
            raise Unavailable("down")
        return await good(ctx)

    return build


async def test_a_retry_mid_install_is_cancelled_when_the_surface_is_removed(
    tmp_path, gateway_plugin, exec_builder, monkeypatch
):
    execs: list = []
    gateway_plugin("t-ok", exec_builder([]))
    gateway_plugin("t-late", _first_fails_then(execs, exec_builder))
    entered, gate = asyncio.Event(), asyncio.Event()
    _held_supervisors(monkeypatch, {"late": (entered, gate)})
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[late]\nplugin = "t-late"\n')
    async with running(reg) as (rt, c):
        await asyncio.wait_for(entered.wait(), 2)  # the retry is inside _install
        retry = rt.table.get("late").retry
        assert retry is not None
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["removed"] == ["late"]
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(retry, 1)  # it unwinds its half-started supervisor
        assert retry.cancelled()
        assert execs[0].closed == 1  # the half-started supervisor closed it
        assert "late" not in rt.table
        assert metrics.REGISTRY.get_sample_value(
            "beherouter_active_sessions", {"surface": "late"}
        ) is None
        assert (await c.get("/healthz")).json() == {"status": "ok", "surfaces": ["a"]}


async def test_a_retry_mid_install_loses_to_a_reload_that_changes_the_surface(
    tmp_path, gateway_plugin, exec_builder, monkeypatch
):
    execs: list = []
    gateway_plugin("t-late", _first_fails_then(execs, exec_builder))
    entered, gate = asyncio.Event(), asyncio.Event()
    _held_supervisors(monkeypatch, {"a": (entered, gate)})
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-late"\n')
    async with running(reg) as (rt, c):
        await asyncio.wait_for(entered.wait(), 2)
        retry = rt.table.get("a").retry
        write(reg, '[a]\nplugin = "t-late"\ncall_timeout_s = 5\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "ok" and result["changed"] == ["a"]
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(retry, 1)
        assert retry.cancelled()
        slot = rt.table.get("a")
        # the slot serves the RELOADED entry's backend, not the retry's
        assert slot.supervisor.backend.executor is execs[1]
        assert (execs[0].closed, execs[1].closed) == (1, 0)
        assert slot.retry is None
        assert (await c.get("/healthz")).json() == {"status": "ok", "surfaces": ["a"]}


async def test_an_added_surface_whose_app_fails_to_start_is_failed_and_retried(
    tmp_path, gateway_plugin, exec_builder, monkeypatch
):
    gateway_plugin("t-ok", exec_builder([]))
    _held_supervisors(monkeypatch, {"b": RuntimeError("lifespan boom")})
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[gone]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n[c]\nplugin = "t-ok"\n')
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "partial" and result["failed"] == ["b"]
        assert (result["added"], result["removed"]) == (["b", "c"], ["gone"])
        body = (await c.get("/healthz")).json()
        assert body["surfaces"] == ["a", "c"] and body["failed"] == ["b"]
        assert set(rt.fingerprints) == {"a", "b", "c"}
        await _until(lambda: rt.table.get("b").app is not None)  # the retry
        assert (await c.get("/healthz")).json() == {
            "status": "ok", "surfaces": ["a", "b", "c"]
        }


async def test_a_retry_config_fault_on_a_serving_surface_says_it_still_serves(
    tmp_path, gateway_plugin, exec_builder, caplog
):
    calls = {"n": 0}
    good = exec_builder([])

    async def build(ctx):
        calls["n"] += 1
        if calls["n"] == 2:  # the reload's attach: transient, so it is retried
            raise Unavailable("down")
        if calls["n"] == 3:  # the retry: a configuration fault
            raise UsageError("bad config")
        return await good(ctx)

    gateway_plugin("t-x", build)
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-x"\n')
    async with running(reg) as (rt, c):
        old = rt.table.get("a").app
        write(reg, '[a]\nplugin = "t-x"\ncall_timeout_s = 5\n')
        await rt.reloader.request("admin")
        await _until(lambda: rt.table.get("a").retry is None)
        assert rt.table.get("a").app is old
        body = (await c.get("/healthz")).json()
        assert body == {"status": "ok", "surfaces": ["a"], "reload_failed": ["a"]}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "previous configuration keeps serving" in text
    assert "serving it as unavailable" not in text


@pytest.fixture
def ks_file(tmp_path, monkeypatch):
    from beherouter.killswitch import _SWITCHES

    p = tmp_path / "ks.json"
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(p))
    _SWITCHES.clear()
    yield p
    _SWITCHES.clear()


async def _call_tool(c, surface: str, tool: str, args: dict) -> str:
    init = await c.post(f"/{surface}/mcp", json=INIT, headers=MCP)
    assert init.status_code == 200, init.text
    headers = {**MCP, "mcp-session-id": init.headers["mcp-session-id"]}
    await c.post(
        f"/{surface}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    r = await c.post(
        f"/{surface}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.text


async def test_a_surface_added_by_reload_is_under_the_kill_switch(
    tmp_path, gateway_plugin, exec_builder, ks_file
):
    import json

    gateway_plugin("t-ok", exec_builder([]))
    ks_file.write_text(json.dumps({"surfaces": {"ks-added": {"reason": "INC-7"}}}))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n[ks-added]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["added"] == ["ks-added"]
        gauge = ("beherouter_surface_disabled", {"surface": "ks-added"})
        assert metrics.REGISTRY.get_sample_value(*gauge) == 1.0
        text = await _call_tool(c, "ks-added", "search_tools", {"query": "x"})
        assert "surface_disabled" in text and "INC-7" not in text
        # the sibling that was there at boot is not stopped
        assert "surface_disabled" not in await _call_tool(c, "a", "search_tools", {"query": "x"})
        ks_file.write_text("{}")
        assert metrics.REGISTRY.get_sample_value(*gauge) == 0.0


async def test_a_removed_surface_loses_its_surface_disabled_gauge(
    tmp_path, gateway_plugin, exec_builder, ks_file
):
    import json

    gateway_plugin("t-ok", exec_builder([]))
    ks_file.write_text(json.dumps({"surfaces": {"ks-gone": {}}}))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[ks-gone]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, _c):
        gauge = ("beherouter_surface_disabled", {"surface": "ks-gone"})
        assert metrics.REGISTRY.get_sample_value(*gauge) == 1.0
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["removed"] == ["ks-gone"]
        assert metrics.REGISTRY.get_sample_value(*gauge) is None
        assert 'beherouter_surface_disabled{surface="ks-gone"}' not in metrics.render()[0].decode()


@pytest.mark.parametrize("body", ["{not json", "[]", '{"nope": {}}'])
async def test_a_malformed_kill_switch_file_refuses_boot(tmp_path, ks_file, body):
    ks_file.write_text(body)
    reg = tmp_path / "r.toml"
    write(reg, "")
    with pytest.raises(UsageError, match="kill-switch"):
        await build_gateway_app(reg)

"""The surface table (spec §2.1): every surface is a swappable slot."""

import asyncio
import contextlib
import json
import logging
import os

import httpx
import pytest

from beherouter.gates import RateLimiter, gates_from_entry
from beherouter.gateway import build_gateway_app, build_surfaces
from beherouter.models import Backend
from beherouter.registry import RegistryEntry
from beherouter.runtime import Supervisor, SurfaceSlot, SurfaceTable, _Counted


class _Exec:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


async def test_unknown_path_is_a_problem_404(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app({})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.get("/nope/mcp")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_a_pending_slot_answers_503_with_retry_after():
    slot = SurfaceSlot("x")
    app = httpx.ASGITransport(app=slot)
    async with httpx.AsyncClient(transport=app, base_url="http://t") as c:
        r = await c.get("/mcp")
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


def test_table_put_and_remove_rebuild_the_routes():
    t = SurfaceTable()
    t.put(SurfaceSlot("a"))
    t.put(SurfaceSlot("b"))
    assert "a" in t and t.names() == ["a", "b"]
    assert len(t.router.routes) == 2
    assert t.remove("a").name == "a"
    assert "a" not in t and len(t.router.routes) == 1


async def test_boot_attached_surfaces_serve_and_close_on_shutdown(
    gateway_plugin, exec_builder, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    execs = []
    gateway_plugin("t-ok", exec_builder(execs))
    app = await build_gateway_app({"a": RegistryEntry(name="a", plugin="t-ok")})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        assert (await c.get("/healthz")).json() == {"status": "ok", "surfaces": ["a"]}
        r = await c.post("/a/mcp", headers={"Authorization": "Bearer s3cret"}, json={})
        assert r.status_code != 404
        assert execs[0].closed == 0
    assert execs[0].closed == 1


def test_rate_limiter_matches_its_own_table():
    rl = RateLimiter.from_table("s", {"calls": 5, "per_s": 60})
    assert rl.matches({"calls": 5, "per_s": 60})
    assert rl.matches({"calls": 5, "per_s": 60.0, "burst": 5})
    assert not rl.matches({"calls": 6, "per_s": 60})


def test_gates_from_entry_reuses_an_equal_limiter():
    limiters: dict = {}
    e = RegistryEntry(name="s", plugin="x", rate_limit={"calls": 5, "per_s": 60})
    first = gates_from_entry(e, limiters).stages[0]
    again = gates_from_entry(e, limiters).stages[0]
    assert again is first
    changed = RegistryEntry(name="s", plugin="x", rate_limit={"calls": 9, "per_s": 60})
    assert gates_from_entry(changed, limiters).stages[0] is not first


async def test_a_swapped_slot_retires_the_old_app_and_closes_its_backend(
    gateway_plugin, exec_builder, monkeypatch
):
    """The move Task 5's reload makes: install a new app in a live slot. The old
    supervisor drains (nothing in flight, so at once) and closes ITS backend only."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    execs = []
    gateway_plugin("t-ok", exec_builder(execs))
    entry = RegistryEntry(name="a", plugin="t-ok")
    app = await build_gateway_app({"a": entry})
    runtime = app.state.runtime
    async with app.router.lifespan_context(app):
        slot = runtime.table.get("a")
        first = slot.app
        backends: dict = {}
        surfaces = await build_surfaces({"a": entry}, backends=backends)
        await runtime._install(slot, surfaces["a"], backends["a"])
        assert slot.app is not None and slot.app is not first
        await asyncio.wait_for(asyncio.gather(*list(runtime._retiring)), 5)
        assert [e.closed for e in execs] == [1, 0]
    assert [e.closed for e in execs] == [1, 1]


async def test_a_drain_ends_with_the_last_request_or_at_the_deadline():
    release = asyncio.Event()

    async def slow(scope, receive, send):
        await release.wait()

    counted = _Counted(slow)
    call = asyncio.create_task(counted({"type": "http"}, None, None))
    await asyncio.sleep(0)
    assert counted.active == 1
    drain = asyncio.create_task(counted.drained(5))
    await asyncio.sleep(0.01)
    assert not drain.done()
    release.set()
    await asyncio.wait_for(drain, 1)
    await call
    assert counted.active == 0

    stuck = _Counted(lambda *a: asyncio.Event().wait())
    hung = asyncio.create_task(stuck({"type": "http"}, None, None))
    await asyncio.sleep(0)
    await asyncio.wait_for(stuck.drained(0.01), 1)  # the deadline, not the request
    hung.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hung


async def test_a_supervisor_whose_app_fails_raises_from_start_and_closes():
    class _Broken:
        def http_app(self, path, stateless_http=False):
            raise RuntimeError("boom")

    ex = _Exec()
    sup = Supervisor("x", _Broken(), Backend(name="x", kind="mcp", descriptors=[], executor=ex))
    with pytest.raises(RuntimeError, match="boom"):
        await sup.start()
    await sup.task
    assert ex.closed == 1


async def test_healthz_reports_the_new_keys_only_when_set(
    gateway_plugin, exec_builder, monkeypatch, tmp_path
):
    """Spec §6.2: none of the four makes status degraded."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    ks = tmp_path / "ks.json"
    ks.write_text(json.dumps({"surfaces": {"a": {}}}))
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(ks))
    gateway_plugin("t-ok", exec_builder([]))
    app = await build_gateway_app({"a": RegistryEntry(name="a", plugin="t-ok")})
    runtime = app.state.runtime
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        body = (await c.get("/healthz")).json()
        assert body == {"status": "ok", "surfaces": ["a"], "disabled": ["a"]}

        runtime.reload_failed.add("a")
        runtime.last_reload_failure = "2026-10-09T00:00:00Z"
        ks.write_text(json.dumps({"all": {}}))
        os.utime(ks, ns=(1, 1))
        body = (await c.get("/healthz")).json()
        assert body["status"] == "ok"
        assert body["reload_failed"] == ["a"]
        assert body["last_reload"] == {"status": "failed", "at": "2026-10-09T00:00:00Z"}
        assert body["disabled"] == ["*"]

        ks.write_text("{not json")
        os.utime(ks, ns=(2, 2))
        body = (await c.get("/healthz")).json()
        assert body["killswitch"] == "stale" and body["disabled"] == ["*"]


async def test_a_failed_boot_install_still_closes_every_other_booted_backend(
    gateway_plugin, exec_builder, monkeypatch
):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    gateway_plugin("t-ok", exec_builder([]))
    app = await build_gateway_app({
        "a": RegistryEntry(name="a", plugin="t-ok"),
        "b": RegistryEntry(name="b", plugin="t-ok"),
    })
    booted = app.state.runtime._booted
    first, second = booted["a"][1].executor, booted["b"][1].executor

    def broken(path, **kw):
        raise RuntimeError("boom")

    booted["a"][0].http_app = broken
    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            pass
    assert (first.closed, second.closed) == (1, 1)


async def test_a_cancelled_start_leaves_no_orphan_supervisor(caplog):
    from starlette.applications import Starlette

    entered = asyncio.Event()

    @contextlib.asynccontextmanager
    async def blocked(app):
        entered.set()
        await asyncio.Event().wait()
        yield

    class _Slow:
        def http_app(self, path, stateless_http=False):
            return Starlette(lifespan=blocked)

    ex = _Exec()
    sup = Supervisor("x", _Slow(), Backend(name="x", kind="mcp", descriptors=[], executor=ex))
    starting = asyncio.create_task(sup.start())
    await asyncio.wait_for(entered.wait(), 1)
    with caplog.at_level(logging.ERROR, logger="beherouter.gateway"):
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
    assert sup.task.done()
    assert ex.closed == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_retired_supervisor_drains_in_flight_requests_up_to_its_deadline():
    """Spec §2.4 at the Supervisor: an in-flight request on a retired app keeps
    the backend open until it finishes, when that is sooner than the drain;
    otherwise the deadline ends the drain and the backend is closed anyway."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return PlainTextResponse("done")

    class _Surface:
        def http_app(self, path, stateless_http=False):
            return Starlette(routes=[Route(path, slow)])

    async def in_flight(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/mcp")

    # Finishes before the deadline: served in full, then the backend closes.
    ex = _Exec()
    sup = Supervisor(
        "x", _Surface(), Backend(name="x", kind="mcp", descriptors=[], executor=ex), drain_s=5
    )
    call = asyncio.create_task(in_flight(await sup.start()))
    await asyncio.wait_for(entered.wait(), 1)
    sup.stop()
    await asyncio.sleep(0.05)
    assert not sup.task.done() and ex.closed == 0  # still draining
    release.set()
    assert (await asyncio.wait_for(call, 1)).text == "done"
    await asyncio.wait_for(sup.task, 1)
    assert ex.closed == 1

    # Never finishes: the deadline ends the drain.
    release.clear()
    entered.clear()
    ex = _Exec()
    sup = Supervisor(
        "y", _Surface(), Backend(name="y", kind="mcp", descriptors=[], executor=ex), drain_s=0.05
    )
    call = asyncio.create_task(in_flight(await sup.start()))
    await asyncio.wait_for(entered.wait(), 1)
    sup.stop()
    await asyncio.wait_for(sup.task, 1)
    assert ex.closed == 1
    call.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await call


async def test_a_supervisor_registers_its_session_series_only_once_installed():
    """A retry cancelled by a reload's `_remove` can leave a supervisor that
    entered its lifespan but was never installed. Registering the
    active_sessions series inside the supervisor let that half-started app
    re-create the series of a REMOVED surface (or repoint a live one at an app
    that never serves). The series is registered at install, never by start()."""
    from fastmcp import FastMCP

    from beherouter import metrics

    labels = {"surface": "race-x"}
    ex = _Exec()
    sup = Supervisor(
        "race-x", FastMCP("x"), Backend(name="race-x", kind="mcp", descriptors=[], executor=ex)
    )
    await sup.start()
    assert metrics.REGISTRY.get_sample_value("beherouter_active_sessions", labels) is None
    sup.track()
    assert metrics.REGISTRY.get_sample_value("beherouter_active_sessions", labels) == 0.0
    sup.stop()
    await asyncio.wait_for(sup.task, 1)
    metrics.forget_surface("race-x")


async def test_install_onto_a_removed_slot_retires_the_new_supervisor():
    from fastmcp import FastMCP

    from beherouter import metrics
    from beherouter.runtime import GatewayRuntime

    rt = GatewayRuntime(
        {}, auth=None, path=None, retry_initial_s=1, retry_max_s=1, drain_s=0
    )
    slot = SurfaceSlot("gone")  # never in the table: removed while starting
    ex = _Exec()
    await rt._install(
        slot, FastMCP("x"), Backend(name="gone", kind="mcp", descriptors=[], executor=ex)
    )
    assert slot.app is None
    await asyncio.wait_for(asyncio.gather(*rt._retiring), 1)
    assert ex.closed == 1
    labels = {"surface": "gone"}
    assert metrics.REGISTRY.get_sample_value("beherouter_active_sessions", labels) is None


async def test_an_app_that_fails_while_serving_is_pending_and_retried(caplog):
    """FastMCP's lifespan runs the session manager's task group; if that group
    crashes, the app is dead. The slot used to keep pointing at it, so
    /healthz and beherouter_surface_up said live while every call failed."""
    import anyio
    from starlette.applications import Starlette

    from beherouter.runtime import GatewayRuntime

    boom = asyncio.Event()

    @contextlib.asynccontextmanager
    async def crashing(app):
        async with anyio.create_task_group() as tg:

            async def crash():
                await boom.wait()
                raise RuntimeError("session manager died")

            tg.start_soon(crash)
            yield

    class _Surface:
        def http_app(self, path, stateless_http=False):
            return Starlette(lifespan=crashing)

    rt = GatewayRuntime(
        {"x": RegistryEntry(name="x", plugin="t-ok")},
        auth=None, path=None, retry_initial_s=3600, retry_max_s=3600, drain_s=0,
    )
    slot = SurfaceSlot("x")
    rt.table.put(slot)
    ex = _Exec()
    await rt._install(slot, _Surface(), Backend(name="x", kind="mcp", descriptors=[], executor=ex))
    sup = slot.supervisor
    assert slot.app is not None
    with caplog.at_level(logging.ERROR, logger="beherouter.gateway"):
        boom.set()
        await asyncio.wait_for(sup.task, 1)
    assert slot.app is None and slot.supervisor is None
    assert "x" in rt.failed
    assert slot.retry is not None  # retried with the entry in force
    assert ex.closed == 1
    slot.retry.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await slot.retry


# --- edges of the table, the supervisor and the runtime -------------------------


def _rt(registry=None, **kw):
    from beherouter.runtime import GatewayRuntime

    kw.setdefault("path", None)
    return GatewayRuntime(
        registry or {}, auth=None, retry_initial_s=0.001, retry_max_s=0.001, drain_s=0, **kw
    )


def _backend(name="x", ex=None):
    return Backend(name=name, kind="mcp", descriptors=[], executor=ex or _Exec())


async def _asgi(app, scope_type):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await app({"type": scope_type}, receive, send)
    return sent


async def test_the_404_closes_a_websocket_and_ignores_other_scopes():
    from beherouter.runtime import _not_found

    assert [m["type"] for m in await _asgi(_not_found, "websocket")] == ["websocket.close"]
    assert await _asgi(_not_found, "lifespan") == []


async def test_a_pending_slot_answers_nothing_outside_http():
    assert await _asgi(SurfaceSlot("x"), "websocket") == []


def test_track_before_the_app_serves_registers_no_series():
    from beherouter import metrics

    Supervisor("never-served", object(), _backend()).track()
    labels = {"surface": "never-served"}
    assert metrics.REGISTRY.get_sample_value("beherouter_active_sessions", labels) is None


async def test_a_lifespan_that_swallows_the_cancel_never_serves():
    """start()'s caller gave up; a lifespan that ignores the cancel and enters
    anyway must not hand out an app, and the backend is still closed."""
    from starlette.applications import Starlette

    entered = asyncio.Event()

    @contextlib.asynccontextmanager
    async def stubborn(app):
        entered.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(10)
        yield

    class _Surface:
        def http_app(self, path, stateless_http=False):
            return Starlette(lifespan=stubborn)

    ex = _Exec()
    sup = Supervisor("x", _Surface(), _backend(ex=ex))
    starting = asyncio.create_task(sup.start())
    await asyncio.wait_for(entered.wait(), 1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert sup.task.done() and not sup.task.cancelled()
    assert sup._app is None
    assert ex.closed == 1


async def test_cancelling_the_supervisor_task_while_it_starts_cancels_start():
    from starlette.applications import Starlette

    entered = asyncio.Event()

    @contextlib.asynccontextmanager
    async def blocked(app):
        entered.set()
        await asyncio.Event().wait()
        yield

    class _Slow:
        def http_app(self, path, stateless_http=False):
            return Starlette(lifespan=blocked)

    ex = _Exec()
    sup = Supervisor("x", _Slow(), _backend(ex=ex))
    starting = asyncio.create_task(sup.start())
    await asyncio.wait_for(entered.wait(), 1)
    sup.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, 1)
    assert ex.closed == 1


async def test_an_uninstalled_app_failing_while_serving_is_only_logged(caplog):
    import anyio
    from starlette.applications import Starlette

    boom = asyncio.Event()

    @contextlib.asynccontextmanager
    async def crashing(app):
        async with anyio.create_task_group() as tg:

            async def crash():
                await boom.wait()
                raise RuntimeError("session manager died")

            tg.start_soon(crash)
            yield

    class _Surface:
        def http_app(self, path, stateless_http=False):
            return Starlette(lifespan=crashing)

    ex = _Exec()
    sup = Supervisor("x", _Surface(), _backend(ex=ex))
    await sup.start()
    with caplog.at_level(logging.ERROR, logger="beherouter.gateway"):
        boom.set()
        await asyncio.wait_for(sup.task, 1)
    assert any("failed while serving" in r.getMessage() for r in caplog.records)
    assert ex.closed == 1


async def test_boot_with_a_file_and_no_stamp_takes_its_own_baseline(tmp_path):
    path = tmp_path / "registry.toml"
    path.write_text("")
    rt = _rt(path=path)
    await rt.boot()
    assert rt._watch_baseline is not None and len(rt._watch_baseline) == 1


async def test_the_watch_outlives_a_reload_that_raises(tmp_path, caplog):
    path = tmp_path / "registry.toml"
    path.write_text("")
    rt = _rt(path=path)
    raised = asyncio.Event()

    async def boom(trigger):
        raised.set()
        raise RuntimeError("reload bug")

    rt.reloader.request = boom
    with caplog.at_level(logging.ERROR, logger="beherouter.gateway"):
        task = asyncio.create_task(rt._watch(0.001, ("stale",)))
        await asyncio.wait_for(raised.wait(), 1)
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any("the reload raised" in r.getMessage() for r in caplog.records)


async def test_triggers_without_sighup_and_without_boot_still_watch(
    tmp_path, monkeypatch, caplog
):
    """Off the main thread there is no SIGHUP; the watch still starts, from a
    baseline taken now when boot() never ran."""
    path = tmp_path / "registry.toml"
    path.write_text("")
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", "60")
    loop = asyncio.get_running_loop()

    def no_signals(*a):
        raise RuntimeError("not the main thread")

    monkeypatch.setattr(loop, "add_signal_handler", no_signals)
    rt = _rt(path=path)
    with caplog.at_level(logging.INFO, logger="beherouter.gateway"):
        rt._install_triggers()
    assert not rt._sighup
    assert any("SIGHUP reload unavailable" in r.getMessage() for r in caplog.records)
    assert rt._watch_task is not None
    await rt.shutdown()
    assert rt._watch_task is None


async def test_a_failure_of_a_supervisor_that_no_longer_holds_its_slot_is_ignored():
    rt = _rt({"x": RegistryEntry(name="x", plugin="t-ok")})
    removed = SurfaceSlot("x")  # not in the table
    rt._serving_failed(removed, Supervisor("x", object(), _backend()))
    assert rt.failed == {} and removed.retry is None


async def test_a_serving_failure_of_a_removed_entry_is_not_retried():
    rt = _rt()  # the entry is no longer in the registry in force
    slot = SurfaceSlot("x")
    rt.table.put(slot)
    sup = Supervisor("x", object(), _backend())
    slot.app, slot.supervisor = object(), sup
    rt._serving_failed(slot, sup)
    assert slot.app is None and "x" in rt.failed
    assert slot.retry is None


def test_retiring_a_supervisor_that_never_started_tracks_nothing():
    rt = _rt()
    sup = Supervisor("x", object(), _backend())
    rt._retire(sup)
    assert sup._stop.is_set() and rt._retiring == set()


def test_removing_an_unknown_surface_is_a_no_op():
    rt = _rt()
    rt._remove("nope")
    assert rt.table.names() == []


async def test_a_retry_whose_app_fails_to_start_tries_again(monkeypatch):
    from beherouter import runtime as runtime_mod

    entry = RegistryEntry(name="x", plugin="t-ok")
    rt = _rt({"x": entry})
    slot = SurfaceSlot("x")
    rt.table.put(slot)
    rt.failed["x"] = "Unavailable: down"

    async def attach(name, entry, auth, pinned_missing, attached, limiters):
        attached[name] = _backend()
        return object()

    installs = []

    async def install(slot, surface, backend, *, stateless=False):
        installs.append(surface)
        if len(installs) == 1:
            raise RuntimeError("lifespan failed")
        slot.app = object()

    monkeypatch.setattr(runtime_mod, "_attach_one", attach)
    rt._install = install
    # Run outside slot.retry: _retry_done leaves a handle that is not its own.
    other = asyncio.create_task(asyncio.sleep(0))
    slot.retry = other
    await asyncio.wait_for(rt._retry(slot, entry), 1)
    assert len(installs) == 2
    assert slot.retry is other
    assert "x" not in rt.failed
    await other


async def test_a_reload_cancelled_inside_an_attach_closes_what_it_built(
    tmp_path, monkeypatch
):
    from beherouter import runtime as runtime_mod

    entries = {
        "a": RegistryEntry(name="a", plugin="t-ok"),
        "b": RegistryEntry(name="b", plugin="t-ok"),
    }
    monkeypatch.setattr(runtime_mod, "load_registry", lambda path: entries)
    monkeypatch.setattr(runtime_mod, "check_registry", lambda new: None)
    built_ex = _Exec()

    async def attach(name, entry, auth, pinned_missing, built, limiters):
        if name == "a":
            raise asyncio.CancelledError
        built[name] = _backend(name, built_ex)
        return object()

    monkeypatch.setattr(runtime_mod, "_attach_one", attach)
    rt = _rt(path=tmp_path / "registry.toml")
    with pytest.raises(asyncio.CancelledError):
        await rt.reload("test")
    assert built_ex.closed == 1
    assert rt.registry == {}

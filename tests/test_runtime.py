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
        def http_app(self, path):
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

    def broken(path):
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
        def http_app(self, path):
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
        def http_app(self, path):
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

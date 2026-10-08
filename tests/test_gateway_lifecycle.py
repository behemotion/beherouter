"""A backend's resources live exactly as long as the backend.

Executors may own pooled clients (`InprocExecutor`, `CalendarExecutor`) and
expose `async aclose()`. Whoever drops a backend closes it: the gateway on
shutdown and whenever attach builds one and then refuses it, `health --deep`
once a check is done with it. A failing `aclose` is logged, never raised over
the error that is actually being reported.
"""

import asyncio
import logging

import httpx
import pytest

from beherouter.errors import Unavailable, UsageError
from beherouter.gateway import build_gateway_app, build_surfaces, close_backend
from beherouter.health import check_entry
from beherouter.models import Backend
from beherouter.plugins import PLUGINS, register
from beherouter.plugins.spec import IdentitySupport, PluginContext, PluginSpec
from beherouter.registry import RegistryEntry


class _Exec:
    """A fake executor that records its aclose calls."""

    def __init__(self, *, fail_run=False, fail_close=False, identity_aware=None):
        self.closed = 0
        self.fail_run = fail_run
        self.fail_close = fail_close
        if identity_aware is not None:
            self.identity_aware = identity_aware

    async def run(self, verb, args, *, identity=None):
        if self.fail_run:
            raise Unavailable("probe went nowhere")
        return {"result": "ok"}

    async def aclose(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("close blew up")


def _backend(name: str, executor) -> Backend:
    return Backend(name=name, kind="mcp", descriptors=[], executor=executor)


@pytest.fixture
def test_plugin():
    names: list[str] = []

    def _register(name: str, build, **spec) -> None:
        register(PluginSpec(name=name, summary="test", backing="inproc", **spec), build)
        names.append(name)

    yield _register
    for n in names:
        PLUGINS.pop(n, None)


def _builder(executors: list, **kw):
    async def build(ctx: PluginContext):
        ex = _Exec(**kw)
        executors.append(ex)
        return _backend(ctx.surface, ex)

    return build


# --- close_backend -------------------------------------------------------------


async def test_close_backend_tolerates_an_executor_without_aclose():
    class _Bare:
        async def run(self, verb, args, *, identity=None):
            return {}

    await close_backend(_backend("x", _Bare()))  # no AttributeError


async def test_close_backend_logs_and_swallows_an_aclose_failure(caplog):
    ex = _Exec(fail_close=True)
    with caplog.at_level(logging.WARNING, logger="beherouter.gateway"):
        await close_backend(_backend("x", ex))
    assert ex.closed == 1
    assert "close blew up" in caplog.text or "'x'" in caplog.text


# --- attach drops ------------------------------------------------------------------


async def test_an_identity_refusal_after_build_closes_the_backend(test_plugin):
    executors: list = []
    test_plugin(
        "t-blind",
        _builder(executors, identity_aware=False),
        identity=IdentitySupport(modes=("client",), target="header"),
    )
    entry = RegistryEntry(
        name="blind",
        plugin="t-blind",
        identity={"mode": "client", "map": {"authorization": "x-token"}},
    )
    with pytest.raises(UsageError, match="cannot apply"):
        await build_surfaces({"blind": entry})
    assert [e.closed for e in executors] == [1]


async def test_an_identity_refusal_is_reported_even_when_aclose_fails(test_plugin):
    executors: list = []
    test_plugin(
        "t-blind",
        _builder(executors, identity_aware=False, fail_close=True),
        identity=IdentitySupport(modes=("client",), target="header"),
    )
    entry = RegistryEntry(
        name="blind",
        plugin="t-blind",
        identity={"mode": "client", "map": {"authorization": "x-token"}},
    )
    with pytest.raises(UsageError, match="cannot apply"):
        await build_surfaces({"blind": entry})
    assert executors[0].closed == 1


async def test_a_cancel_after_build_closes_the_backend(test_plugin, monkeypatch):
    """Cancelled while costing the surface (shutdown during a retry attach):
    the backend was built, so it is closed, and the cancel still propagates."""
    from beherouter import costing

    executors: list = []
    test_plugin("t-ok", _builder(executors))
    entered = asyncio.Event()

    async def hang(*a, **k):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(costing, "surface_cost", hang)
    task = asyncio.create_task(
        build_surfaces({"s": RegistryEntry(name="s", plugin="t-ok")}, failed={})
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e.closed for e in executors] == [1]


async def test_a_raising_build_surfaces_closes_the_surfaces_it_did_attach(test_plugin):
    """Without `failed`, one failure raises — and the surfaces that DID attach
    are dropped with it, so their backends are closed too."""
    executors: list = []
    test_plugin("t-ok", _builder(executors))

    async def boom(ctx):
        raise Unavailable("down")

    test_plugin("t-boom", boom)
    registry = {
        "ok": RegistryEntry(name="ok", plugin="t-ok"),
        "bad": RegistryEntry(name="bad", plugin="t-boom"),
    }
    with pytest.raises(Unavailable, match="down"):
        await build_surfaces(registry)
    assert [e.closed for e in executors] == [1]


async def test_a_successful_attach_does_not_close_the_backend(test_plugin):
    executors: list = []
    test_plugin("t-ok", _builder(executors))
    await build_surfaces({"ok": RegistryEntry(name="ok", plugin="t-ok")}, failed={})
    assert [e.closed for e in executors] == [0]


# --- gateway shutdown ----------------------------------------------------------------


async def _wait_ok(c) -> None:
    for _ in range(200):
        if (await c.get("/healthz")).json()["status"] == "ok":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("surface never attached")


async def test_shutdown_closes_every_attached_backend_once(test_plugin, monkeypatch):
    """Boot-attached and retry-attached alike, after the app stops serving."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    executors: list = []
    test_plugin("t-ok", _builder(executors))
    calls = {"n": 0}
    good = _builder(executors)

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable("not yet")
        return await good(ctx)

    test_plugin("t-flaky", flaky)
    registry = {
        "a": RegistryEntry(name="a", plugin="t-ok"),
        "b": RegistryEntry(name="b", plugin="t-ok"),
        "late": RegistryEntry(name="late", plugin="t-flaky"),
    }
    app = await build_gateway_app(registry, retry_initial_s=0.01, retry_max_s=0.05)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        await _wait_ok(c)
        assert len(executors) == 3
        assert all(e.closed == 0 for e in executors)  # open while serving
    assert [e.closed for e in executors] == [1, 1, 1]


async def test_a_failing_aclose_does_not_stop_the_others_closing(test_plugin, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    executors: list = []
    test_plugin("t-bad-close", _builder(executors, fail_close=True))
    test_plugin("t-ok", _builder(executors))
    registry = {
        "x": RegistryEntry(name="x", plugin="t-bad-close"),
        "y": RegistryEntry(name="y", plugin="t-ok"),
    }
    app = await build_gateway_app(registry)
    async with app.router.lifespan_context(app):
        pass
    assert sorted(e.closed for e in executors) == [1, 1]


# --- health ----------------------------------------------------------------------------


async def test_check_entry_closes_the_backend_after_a_good_probe():
    ex = _Exec()

    async def load(entry):
        return _backend(entry.name, ex)

    entry = RegistryEntry(name="h", plugin="none", pinned=[], probe="ping")
    record = await check_entry(entry, load=load)
    assert record["probe"] == "ok"
    assert ex.closed == 1


async def test_check_entry_closes_the_backend_after_a_failed_probe():
    ex = _Exec(fail_run=True, fail_close=True)

    async def load(entry):
        return _backend(entry.name, ex)

    entry = RegistryEntry(name="h", plugin="none", pinned=[], probe="ping")
    record = await check_entry(entry, load=load)
    # the real error is reported, not the close failure
    assert record["probe"] == "failed"
    assert "probe went nowhere" in record["error"]
    assert ex.closed == 1

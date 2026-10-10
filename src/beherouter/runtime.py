"""The live surface table. Every surface is a SLOT whose app can be swapped.

Each surface is a Mount whose app is its SLOT, never the surface's app
itself, and the router's `routes` list is replaced whole on every change: the
fixed routes (/healthz, /metrics) plus a Mount per slot. A request sees the old
list or the new one, never half of each. Once the gateway `bind`s the table to
its own router, the surfaces are the app's top-level routes exactly as they
were before the table existed (`app.routes` still lists `/<name>`), and an
unknown name answers an RFC 9457 404 instead of Starlette's plain text.

Boot, retry and reload reduce to one move: start a `Supervisor`, which builds
the surface's http_app, holds its lifespan in ITS OWN task and installs the
app in the slot.
⚠️ The lifespan must be entered and exited in the same task. Pushing it onto
the parent's exit stack exits an anyio task group from another task, and that
raises.

The published `tools` array still freezes per app instance. An unchanged
surface is never rebuilt, so its hosts' prompt caches survive a reload.
"""

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Router
from starlette.types import ASGIApp
from starlette.websockets import WebSocketClose

from . import killswitch, metrics
from .errors import UsageError
from .gateway import (
    _attach_one,
    _close_all,
    _log_attach_failure,
    build_surfaces,
    check_registry,
    close_backend,
    is_config_fault,
)
from .logsetup import timestamp
from .registry import load_registry
from .reload import Reloader, diff, fingerprint, watch_s, watch_stamp, watched_paths

logger = logging.getLogger("beherouter.gateway")


def problem(status: int, title: str, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
        headers=headers or {},
    )


async def _not_found(scope, receive, send) -> None:
    if scope["type"] == "websocket":
        await WebSocketClose()(scope, receive, send)
        return
    if scope["type"] != "http":
        return
    await problem(404, "Not found", "no such surface on this gateway")(scope, receive, send)


class _Counted:
    """An installed app that counts its in-flight requests, so a drain can end
    as soon as the last one finishes instead of always waiting the deadline."""

    def __init__(self, app) -> None:
        self.app = app
        self.active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(self, scope, receive, send) -> None:
        self.active += 1
        self._idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
            if self.active == 0:
                self._idle.set()

    async def drained(self, timeout_s: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await self._idle.wait()


class SurfaceSlot:
    """One surface's place in the table. While `app` is None it answers the
    RFC 9457 503 that `_PendingSurface` used to (body names the surface only:
    an attach error can carry internal hostnames, and this path is reachable
    before authentication). A `config_fault` slot is never retried, so its 503
    carries no Retry-After."""

    def __init__(self, name: str, *, config_fault: bool = False) -> None:
        self.name = name
        self.app: ASGIApp | None = None
        self.supervisor: Supervisor | None = None
        self.retry: asyncio.Task | None = None
        self.config_fault = config_fault

    async def __call__(self, scope, receive, send) -> None:
        app = self.app
        if app is not None:
            await app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        if self.config_fault:
            detail = (
                f"surface '{self.name}' is not attached and will not be retried: "
                f"its configuration must be fixed"
            )
            headers = None
        else:
            detail = f"surface '{self.name}' is not attached; the gateway is retrying"
            headers = {"Retry-After": "30"}
        await problem(503, "Surface unavailable", detail, headers)(scope, receive, send)


class SurfaceTable:
    def __init__(self) -> None:
        self.slots: dict[str, SurfaceSlot] = {}
        self.router = Router(routes=[], default=_not_found)
        self._fixed: list[BaseRoute] = []

    def bind(self, router: Router, fixed: Sequence[BaseRoute]) -> None:
        """Publish onto `router` (the app's own) from now on: `fixed` first, so
        a surface can never shadow /healthz or /metrics, then a Mount per slot.
        The router's 404 becomes the RFC 9457 one."""
        router.default = _not_found
        self.router, self._fixed = router, list(fixed)
        self._publish(self.slots)

    def _publish(self, slots: dict[str, SurfaceSlot]) -> None:
        self.slots = slots
        self.router.routes = [
            *self._fixed,
            *(Mount(f"/{n}", app=s) for n, s in slots.items()),
        ]

    def __contains__(self, name: object) -> bool:
        return name in self.slots

    def get(self, name: str) -> SurfaceSlot | None:
        return self.slots.get(name)

    def put(self, slot: SurfaceSlot) -> None:
        self._publish({**self.slots, slot.name: slot})

    def remove(self, name: str) -> SurfaceSlot | None:
        slots = dict(self.slots)
        slot = slots.pop(name, None)
        self._publish(slots)
        return slot

    def names(self) -> list[str]:
        return list(self.slots)

    def live(self) -> list[str]:
        return sorted(n for n, s in self.slots.items() if s.app is not None)


def _track_sessions(name: str, app) -> None:
    if not metrics.track_sessions(name, app):
        logger.warning(
            "beherouter_active_sessions omitted for surface %r: this FastMCP "
            "does not expose its session table",
            name,
        )


class Supervisor:
    """Owns one installed app: its lifespan, its drain, its backend's close."""

    def __init__(
        self, name: str, surface, backend, *, drain_s: float = 0.0, stateless: bool = False
    ) -> None:
        self.name = name
        self.surface = surface
        self.backend = backend
        self.drain_s = drain_s
        self.stateless = stateless
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._app = None  # the raw http app, once its lifespan is serving
        # Set by the install: called once if the app dies after it was serving.
        self.on_failed: Callable[[], None] | None = None

    async def start(self) -> ASGIApp:
        """Run the app's lifespan in a new task; return the installed app once
        it is serving. A lifespan failure is raised here.

        Either way the task is never orphaned: on failure it has finished (and
        closed the backend) before this raises; if the CALLER is cancelled while
        the lifespan is entering, the task is cancelled and awaited too, so no
        supervisor outlives the slot that never received it."""
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run(ready), name=f"beherouter-surface-{self.name}")
        self.task = task
        try:
            return await ready
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        except Exception:
            await task  # _run handed its error over; this waits for its close
            raise

    def track(self) -> None:
        """Point the surface's active_sessions series at this app. Called by
        the install that put it in its slot, NEVER from inside `_run`: a
        supervisor whose start() was cancelled (a retry cancelled by a reload's
        `_remove`) must not re-create a removed surface's series, or repoint a
        live one at an app that never serves."""
        if self.stateless:
            metrics.untrack_sessions(self.name)
            logger.info(
                "surface %r is stateless: no MCP sessions, so no "
                "beherouter_active_sessions series",
                self.name,
            )
        elif self._app is not None:
            _track_sessions(self.name, self._app)

    def stop(self) -> None:
        """Stop taking requests is the caller's job (swap the slot first); this
        lets in-flight ones finish for up to drain_s, then closes."""
        self._stop.set()

    async def _run(self, ready: asyncio.Future) -> None:
        try:
            app = self.surface.http_app(path="/mcp", stateless_http=self.stateless)
            async with app.router.lifespan_context(app):
                if ready.done():  # start()'s caller gave up while we entered
                    return
                counted = _Counted(app)
                self._app = app
                ready.set_result(counted)
                await self._stop.wait()
                await counted.drained(self.drain_s)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as e:  # handed to start()'s caller
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.error("surface %r app failed while serving", self.name, exc_info=True)
                if self.on_failed is not None and not self._stop.is_set():
                    self.on_failed()
        finally:
            await close_backend(self.backend)


class GatewayRuntime:
    """The gateway's mutable state: the registry in force, the surface table,
    and the bookkeeping /healthz reports. One per app."""

    def __init__(
        self,
        registry: dict,
        *,
        auth,
        path: Path | None,
        retry_initial_s: float,
        retry_max_s: float,
        drain_s: float,
        registry_stamp: tuple | None = None,
    ) -> None:
        self.registry = registry
        self.path = path
        # The registry file's watch stamp, taken by the caller BEFORE it read the
        # file; None = take it at boot. See boot().
        self._registry_stamp = registry_stamp
        self._watch_baseline: tuple | None = None
        self.auth = auth
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self.drain_s = drain_s
        self.table = SurfaceTable()
        self.failed: dict[str, str] = {}
        self.config_faults: set[str] = set()
        self.pinned_missing: dict[str, list[str]] = {}
        self.reload_failed: set[str] = set()
        self.last_reload_failure: str | None = None
        self.limiters: dict = {}
        self.fingerprints: dict[str, str] = {}
        self.killswitch = killswitch.configured()
        self._booted: dict[str, tuple] = {}  # name -> (surface, backend), until start()
        self._retiring: set[asyncio.Task] = set()
        self.reloader = Reloader(self.reload)
        self._signal_tasks: set[asyncio.Task] = set()
        self._watch_task: asyncio.Task | None = None
        self._sighup = False

    def _track(self, name: str) -> None:
        slot_of = self.table.get

        def up() -> bool:
            slot = slot_of(name)
            return slot is not None and slot.app is not None

        metrics.track_surface_up(name, up)
        if self.killswitch is not None:
            metrics.track_killswitch(name, self.killswitch)

    async def boot(self) -> None:
        """Attach every surface (concurrently, failures isolated). No app is
        served until start() runs inside the lifespan."""
        # Fingerprints and the watch baseline are taken BEFORE attaching: a
        # ${file:} rotated while build_surfaces runs then differs from both, so
        # the next reload (or watch tick) re-attaches the surface. Taken after,
        # they would match the new file while the app runs on the old value,
        # and nothing would ever re-attach it. (And a fingerprint that raises
        # does so before any backend exists to leak.)
        for name, entry in self.registry.items():
            self.fingerprints[name] = fingerprint(entry)
        if self.path is not None:
            stamp = watch_stamp(watched_paths(self.path, self.registry))
            if self._registry_stamp is not None:
                stamp = self._registry_stamp + stamp[1:]
            self._watch_baseline = stamp
        backends: dict = {}
        surfaces = await build_surfaces(
            self.registry,
            auth=self.auth,
            failed=self.failed,
            config_faults=self.config_faults,
            pinned_missing=self.pinned_missing,
            backends=backends,
            limiters=self.limiters,
        )
        for name in self.registry:
            self.table.put(SurfaceSlot(name, config_fault=name in self.config_faults))
            self._track(name)
            if name in surfaces:
                self._booted[name] = (surfaces[name], backends[name])

    async def start(self) -> None:
        """Inside the lifespan: install every boot-attached app, start retries."""
        # One at a time: if an install fails, the ones not yet popped stay in
        # _booted for shutdown() to close, and none is closed twice (a failed
        # install's supervisor has already closed its own backend).
        for name in list(self._booted):
            surface, backend = self._booted.pop(name)
            await self._install(
                self.table.slots[name],
                surface,
                backend,
                stateless=bool(self.registry[name].stateless),
            )
        for name in self.failed:
            if name not in self.config_faults:
                slot = self.table.slots[name]
                slot.retry = asyncio.create_task(
                    self._retry(slot, self.registry[name]), name=f"beherouter-retry-{name}"
                )
        self._install_triggers()

    def on_sighup(self) -> None:
        """The SIGHUP callback: fire and forget, the outcome is logged by reload."""
        task = asyncio.ensure_future(self.reloader.request("sighup"))
        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)

    async def _watch(self, interval: float, last: tuple) -> None:
        """Poll the registry file and every ${file:} it references (spec §2.2).
        `last` is the baseline boot() took before attaching, so an edit made
        during the attach or right after start() is not mistaken for it."""
        path = self.path
        assert path is not None  # only started with a file to watch
        while True:
            await asyncio.sleep(interval)
            stamp = watch_stamp(watched_paths(path, self.registry))
            if stamp == last:
                continue
            # Updated BEFORE the reload: a registry that fails lint is reported
            # once, not retried on every tick, and an edit landing during the
            # reload is not swallowed. A reload that changes which ${file:}
            # paths are watched costs one idempotent follow-up.
            last = stamp
            try:
                await self.reloader.request("watch")
            except Exception:  # the watch must outlive one bad reload
                logger.error("registry watch: the reload raised", exc_info=True)

    def _install_triggers(self) -> None:
        if self.path is None:
            return
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, self.on_sighup)
            self._sighup = True
        except (NotImplementedError, RuntimeError, ValueError):
            logger.info("SIGHUP reload unavailable in this process (not the main thread)")
        interval = watch_s()
        if interval is not None:
            baseline = self._watch_baseline
            if baseline is None:  # start() without boot()
                baseline = watch_stamp(watched_paths(self.path, self.registry))
            self._watch_task = asyncio.create_task(
                self._watch(interval, baseline), name="beherouter-registry-watch"
            )

    async def _install(
        self, slot: SurfaceSlot, surface, backend, *, stateless: bool = False
    ) -> None:
        sup = Supervisor(
            slot.name, surface, backend, drain_s=self.drain_s, stateless=stateless
        )
        app = await sup.start()
        if self.table.get(slot.name) is not slot:
            # Removed while its app was starting: never install, never track.
            self._retire(sup)
            return
        old = slot.supervisor
        slot.app, slot.supervisor, slot.config_fault = app, sup, False
        sup.track()
        sup.on_failed = lambda: self._serving_failed(slot, sup)
        if old is not None:
            self._retire(old)

    def _serving_failed(self, slot: SurfaceSlot, sup: Supervisor) -> None:
        """An installed app died while serving: answer 503 again, as a pending
        surface does, and retry the entry in force. Only if `sup` still holds
        the slot -- a retired or replaced app failing is no one's concern."""
        if self.table.get(slot.name) is not slot or slot.supervisor is not sup:
            return
        slot.app, slot.supervisor = None, None
        metrics.untrack_sessions(slot.name)
        self.failed[slot.name] = "RuntimeError: the surface's app failed while serving"
        entry = self.registry.get(slot.name)
        if entry is not None and slot.retry is None:
            slot.retry = asyncio.create_task(
                self._retry(slot, entry), name=f"beherouter-retry-{slot.name}"
            )

    def _retire(self, sup: Supervisor) -> None:
        sup.stop()
        if sup.task is not None:
            self._retiring.add(sup.task)
            sup.task.add_done_callback(self._retiring.discard)

    def _cancel_retry(self, slot: SurfaceSlot) -> None:
        """Cancel without waiting: a retry cancelled mid-install unwinds its
        half-started supervisor (closing the backend) on its own. Tracked with
        the retiring supervisors, so shutdown still waits for it."""
        if slot.retry is not None:
            task, slot.retry = slot.retry, None
            task.cancel()
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)

    async def _retry(self, slot: SurfaceSlot, entry) -> None:
        """Retry one attach with exponential backoff and install it ONCE. A
        transient failure is retried forever (delay capped at retry_max_s); a
        configuration fault ends the loop.

        `slot.retry` stays set until the install is DONE: a reload that removes
        or changes the surface cancels this task, and a cancel that lands while
        the supervisor is starting is safe (it closes its own backend). Cleared
        before the install, the task could not be cancelled and its install
        would land on a removed slot, or race the reload's own."""
        delay = self.retry_initial_s
        while True:
            await asyncio.sleep(delay)
            attached: dict = {}
            try:
                surface = await _attach_one(
                    slot.name, entry, self.auth, self.pinned_missing, attached, self.limiters
                )
            except Exception as e:
                if slot.app is None:
                    self.failed[slot.name] = f"{type(e).__name__}: {e}"
                if is_config_fault(e):
                    if slot.app is None:
                        slot.config_fault = True
                        self.config_faults.add(slot.name)
                        _log_attach_failure(slot.name, e)
                    else:
                        logger.error(
                            "surface %r: its reloaded entry has a configuration fault; "
                            "the previous configuration keeps serving and the new entry "
                            "is NOT retried — fix the registry, then reload",
                            slot.name,
                            exc_info=e,
                        )
                    self._retry_done(slot)
                    return
                delay = min(delay * 2, self.retry_max_s)
                logger.warning(
                    "surface %r still failing to attach; next try in %gs",
                    slot.name,
                    delay,
                    exc_info=True,
                )
                continue
            try:
                await self._install(
                    slot, surface, attached[slot.name], stateless=bool(entry.stateless)
                )
            except Exception:
                # the app's lifespan failed; its supervisor closed the backend
                delay = min(delay * 2, self.retry_max_s)
                logger.warning(
                    "surface %r attached on retry but its app failed to start; next "
                    "try in %gs",
                    slot.name,
                    delay,
                    exc_info=True,
                )
                continue
            self._retry_done(slot)
            self.failed.pop(slot.name, None)
            self.config_faults.discard(slot.name)
            self.reload_failed.discard(slot.name)
            logger.info("surface %r attached on retry", slot.name)
            return

    @staticmethod
    def _retry_done(slot: SurfaceSlot) -> None:
        # only this task's own handle: a reload may already have replaced it
        if slot.retry is asyncio.current_task():
            slot.retry = None

    async def reload(self, trigger: str) -> dict:
        """One reload (spec §2.3). Never raises for an operator mistake: the
        outcome says what happened, and the log carries the detail.

        Run it through `self.reloader`, never directly: two reloads at once
        would race on the table."""
        result: dict = {
            "trigger": trigger, "added": [], "changed": [], "removed": [],
            "unchanged": [], "failed": [],
        }
        try:
            if self.path is None:
                raise UsageError(
                    "the gateway was started from an in-memory registry; there is no "
                    "file to reload"
                )
            new = load_registry(self.path)
            check_registry(new)
            prints = {n: fingerprint(e) for n, e in new.items()}
        except Exception as e:  # noqa: BLE001 -- any refusal keeps the old registry
            self.last_reload_failure = timestamp(time.time())
            error = f"{type(e).__name__}: {e}"
            logger.error(
                "registry reload (%s) refused; the previous registry stays in force: %s",
                trigger,
                error,
            )
            metrics.RELOADS.labels(trigger=trigger, outcome="failed").inc()
            return {**result, "outcome": "failed", "error": error}

        d = diff(self.fingerprints, prints)
        result.update(
            added=d.added, changed=d.changed, removed=d.removed, unchanged=d.unchanged
        )
        targets = [*d.added, *d.changed]
        built: dict = {}  # attached, not yet handed to a supervisor
        try:
            outcomes = await asyncio.gather(
                *(
                    _attach_one(n, new[n], self.auth, self.pinned_missing, built, self.limiters)
                    for n in targets
                ),
                return_exceptions=True,
            )
            for name, res in zip(targets, outcomes, strict=True):
                if isinstance(res, BaseException) and not isinstance(res, Exception):
                    raise res
                slot = self.table.get(name)
                if slot is None:
                    slot = SurfaceSlot(name)
                    self.table.put(slot)
                    self._track(name)
                self._cancel_retry(slot)
                if not isinstance(res, Exception):
                    # popped first: from here on the supervisor owns (and closes) it
                    try:
                        await self._install(
                            slot, res, built.pop(name), stateless=bool(new[name].stateless)
                        )
                    except Exception as e:  # noqa: BLE001 -- its app failed to start
                        result["failed"].append(name)
                        self._attach_failed(slot, new[name], e)
                        continue
                    self.failed.pop(name, None)
                    self.config_faults.discard(name)
                    self.reload_failed.discard(name)
                    if not new[name].rate_limit:
                        self.limiters.pop(name, None)
                    continue
                result["failed"].append(name)
                self._attach_failed(slot, new[name], res)
        except BaseException:
            # only a cancellation (shutdown) gets here: nothing attached may leak
            await _close_all(built.values())
            raise
        for name in d.removed:
            self._remove(name)
        self.registry, self.fingerprints = new, prints
        self.last_reload_failure = None
        outcome = "partial" if result["failed"] else "ok"
        metrics.RELOADS.labels(trigger=trigger, outcome=outcome).inc()
        metrics.RELOAD_LAST_SUCCESS.set(time.time())
        log = logger.warning if result["failed"] else logger.info
        log(
            "registry reload (%s): %s; added %s, changed %s, removed %s, failed %s",
            trigger, outcome, d.added, d.changed, d.removed, result["failed"],
        )
        return {**result, "outcome": outcome}

    def _attach_failed(self, slot: SurfaceSlot, entry, exc: Exception) -> None:
        """A reload's attach of an added or changed entry failed (spec §2.3's
        table): a pending slot stays pending, as at boot; a serving one KEEPS
        serving its previous app. Either way a transient fault retries the NEW
        entry, and a configuration fault does not."""
        name = slot.name
        fault = is_config_fault(exc)
        if slot.app is None:
            _log_attach_failure(name, exc)
            self.failed[name] = f"{type(exc).__name__}: {exc}"
            slot.config_fault = fault
            if fault:
                self.config_faults.add(name)
            else:
                self.config_faults.discard(name)
        else:
            self.reload_failed.add(name)
            logger.warning(
                "surface %r: its reloaded entry failed to attach; still serving "
                "the previous configuration%s",
                name,
                "" if fault else " and retrying the new one",
                exc_info=exc,
            )
        if not fault:
            slot.retry = asyncio.create_task(
                self._retry(slot, entry), name=f"beherouter-retry-{name}"
            )

    def _remove(self, name: str) -> None:
        """Drop a surface: its path 404s at once, its app drains and closes."""
        slot = self.table.remove(name)
        if slot is None:
            return
        self._cancel_retry(slot)
        if slot.supervisor is not None:
            self._retire(slot.supervisor)
        for table in (self.failed, self.pinned_missing, self.limiters):
            table.pop(name, None)
        self.config_faults.discard(name)
        self.reload_failed.discard(name)
        metrics.forget_surface(name)

    async def shutdown(self) -> None:
        """Cancel a reload in flight and the retries, then every supervisor;
        each closes its backend after its app's lifespan has exited. The triggers
        go first, so none can start a reload into a table being torn down."""
        if self._sighup:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
            self._sighup = False
        triggers = [t for t in (self._watch_task, *self._signal_tasks) if t is not None]
        self._watch_task = None
        for t in triggers:
            t.cancel()
        await asyncio.gather(*triggers, return_exceptions=True)
        await self.reloader.cancel()
        retries = [s.retry for s in self.table.slots.values() if s.retry is not None]
        for t in retries:
            t.cancel()
        await asyncio.gather(*retries, return_exceptions=True)
        tasks = [
            s.supervisor.task
            for s in self.table.slots.values()
            if s.supervisor is not None and s.supervisor.task is not None
        ]
        tasks += list(self._retiring)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Attached at boot but never installed (the lifespan never started).
        await _close_all([b for _, b in self._booted.values()])
        self._booted = {}

    def health_body(self) -> dict:
        """/healthz (see gateway.build_gateway_app.healthz for the contract).
        Every key beyond status and surfaces is present only when non-empty."""
        body: dict[str, object] = {
            "status": "degraded" if self.failed else "ok",
            "surfaces": self.table.live(),
        }
        if self.failed:
            body["failed"] = sorted(self.failed)
        stuck = sorted(n for n in self.config_faults if n in self.failed)
        if stuck:
            body["needs_config_change"] = stuck
        if self.pinned_missing:
            body["pinned_missing"] = {
                n: self.pinned_missing[n] for n in sorted(self.pinned_missing)
            }
        if self.reload_failed:
            body["reload_failed"] = sorted(self.reload_failed)
        if self.last_reload_failure is not None:
            body["last_reload"] = {"status": "failed", "at": self.last_reload_failure}
        if self.killswitch is not None:
            state = self.killswitch.state()
            if state.all is not None:
                body["disabled"] = ["*"]
            elif state.surfaces:
                body["disabled"] = sorted(state.surfaces)
            if self.killswitch.stale:
                body["killswitch"] = "stale"
        return body

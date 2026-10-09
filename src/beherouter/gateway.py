"""Assemble all surfaces from the registry and serve them under /<tool>/mcp behind auth."""

import asyncio
import contextlib
import logging
import math
import os
from pathlib import Path

from fastmcp import FastMCP

from . import metrics
from .audit import audit_enabled
from .auth import (
    AUTH_MODE_VAR,
    OIDC_ROLES_CLAIM_VAR,
    ObservedVerifier,
    RejectionMiddleware,
    auth_mode,
    build_verifier,
    roles_claim,
)
from .envexpand import expand
from .errors import Unavailable, UsageError
from .registry import RegistryEntry, load_registry, validate_entry
from .surface import build_surface

logger = logging.getLogger(__name__)

DEFAULT_PORT = 47100
DEFAULT_HOST = "0.0.0.0"

ATTACH_TIMEOUT_VAR = "BEHEROUTER_ATTACH_TIMEOUT_S"
DEFAULT_ATTACH_TIMEOUT_S = 30.0


def attach_timeout_s() -> float:
    """How long one surface's attach may take before it counts as failed.

    Bounded because an MCP attach does network I/O (`list_tools`) and a hung
    backend would otherwise hold startup forever. A bad value is refused at
    boot, like every other configuration mistake.
    """
    raw = os.environ.get(ATTACH_TIMEOUT_VAR, "")
    if not raw:
        return DEFAULT_ATTACH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise UsageError(
            f"{ATTACH_TIMEOUT_VAR} must be a positive number of seconds, got {raw!r}"
        )
    return value


CALL_TIMEOUT_VAR = "BEHEROUTER_CALL_TIMEOUT_S"


def default_call_timeout_s() -> float | None:
    """The gateway-wide call limit, or None — no limit, the default."""
    raw = os.environ.get(CALL_TIMEOUT_VAR, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        raise UsageError(
            f"{CALL_TIMEOUT_VAR} must be a positive number of seconds, got {raw!r}"
        )
    return value


def call_timeout_s(entry: RegistryEntry) -> float | None:
    if entry.call_timeout_s is not None:
        return float(entry.call_timeout_s)
    return default_call_timeout_s()


def preflight(registry: dict[str, RegistryEntry]) -> None:
    """Refuse to boot on what `registry-lint` can see. No network I/O.

    The one read is local: a `${file:/…}` placeholder is resolved (by `expand`),
    so a missing, unreadable or empty secret file refuses boot here.

    These are operator mistakes with a local fix — an unknown plugin, a bad
    config type, an unset `${VAR}` — and a gateway that booted around them
    would hide them behind a degraded surface. Everything else a surface can
    fail on is found only by attaching, and is isolated to that surface.
    """
    attach_timeout_s()
    default_call_timeout_s()
    # Read again per surface by AuditSink.from_env; refused here so a bad value
    # is one refused boot, not every surface listed under needs_config_change.
    # (BEHEROUTER_LOG_FORMAT needs no check here: serve() runs
    # logsetup.configure() first, which refuses it.)
    audit_enabled()
    for name, entry in registry.items():
        validate_entry(entry)
        if entry.env:
            expand(name, entry.env)


async def load_backend(entry: RegistryEntry):
    """Resolve a registry entry through its plugin.

    Only the final `build` may perform I/O; everything before it is validation
    against inert data.
    """
    from .plugins import get, resolve_aliases, resolve_pinned, resolve_probe
    from .plugins.spec import PluginContext
    from .plugins.validate import validate_config

    validate_entry(entry)
    plugin = get(entry.plugin)
    probe, probe_args = resolve_probe(entry, plugin)
    ctx = PluginContext(
        surface=entry.name,
        config=validate_config(entry.name, plugin.spec, entry.config),
        env=expand(entry.name, entry.env) if entry.env else {},
        pinned=resolve_pinned(entry, plugin),
        probe=probe,
        probe_args=probe_args,
    )
    backend = await plugin.build(ctx)
    # Resolved here rather than threaded through every plugin's build(): the
    # TTL is a gateway concern, not a backend-specific one, and four plugins
    # would otherwise each have to forward a value none of them interpret.
    backend.ttl_ms = (
        entry.catalogue_ttl_ms
        if entry.catalogue_ttl_ms is not None
        else plugin.spec.catalogue_ttl_ms
    )
    # Same reasoning as ttl_ms: vocabulary is a search concern, not a backend's.
    backend.search_aliases = resolve_aliases(entry, plugin)
    return backend


def missing_pins(backend, expected: list[str]) -> list[str]:
    """The names in `expected` (a resolved pin list) the backend does not serve.

    ONE helper for the gateway's attach-time warning and `health --deep`'s
    `pinned_missing` verdict, so the two cannot disagree on what "served" means.
    ⚠️ `cli` descriptors pin by BARE VERB while their published name is
    `flatten(surface, verb)` (see `load_cli_backend`); comparing a `cli` pin
    list against `d.name` would report every healthy `cli` surface as missing.
    """
    served = {d.verb if backend.kind == "cli" else d.name for d in backend.descriptors}
    return sorted(n for n in expected if n not in served)


async def close_backend(backend) -> None:
    """Release what a backend's executor holds (pooled clients). Never raises.

    `aclose` is optional — only executors that own resources expose it
    (`InprocExecutor`, `CalendarExecutor`). Whoever drops a backend calls this:
    the gateway on shutdown and when attach refuses a backend it already built,
    `health --deep` once a check is done. A close failure is logged and
    swallowed, because the caller is always reporting something more important
    (a refusal, a probe verdict, a clean shutdown of every other surface).
    """
    aclose = getattr(backend.executor, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.warning(
            "closing backend %r failed", getattr(backend, "name", "?"), exc_info=True
        )


async def _close_all(backends) -> None:
    """Close each backend in turn; one failure never skips the rest."""
    for backend in backends:
        await close_backend(backend)


def is_config_fault(exc: BaseException) -> bool:
    """Whether an attach failure will not fix itself by waiting.

    A UsageError at attach is a configuration fault (a refused identity mode, a
    config value only the plugin's build can judge): retrying it with backoff
    only fills the log while the surface stays down. Everything else — a
    refused connection, a timeout (raised as Unavailable), a backend 5xx — may
    clear on its own, so it is retried.
    """
    return isinstance(exc, UsageError)


async def _attach_one(
    name: str,
    entry: RegistryEntry,
    auth: object | None,
    pinned_missing: dict[str, list[str]] | None = None,
    backends: dict | None = None,
    limiters: dict | None = None,
) -> FastMCP:
    """Attach one surface: backend, policy, surface, cost instructions.

    On success the backend is recorded in `backends[name]` (when given), so
    whoever owns the surface can close it later. `limiters` is the gateway's
    `{surface: RateLimiter}` (`gates_from_entry`). A backend built and then
    dropped HERE — refused, or cancelled mid-attach — is closed before the
    error propagates.

    `instructions` is set HERE rather than in build_surface because computing
    the cost needs FastMCP.list_tools(), which is async, and build_surface is
    not. A host reads instructions at `initialize`, so it learns what the
    surface costs without spending a tool call.

    Computing the cost is NOT allowed to fail the attach: `naive_tokens`
    re-registers every descriptor a backend has through the signature-synthesis
    path, so a schema edge case in one unpinned tool would otherwise fail the
    whole surface. It degrades to a plain, cost-free `instructions` (logged).
    """
    timeout = attach_timeout_s()
    try:
        backend = await asyncio.wait_for(load_backend(entry), timeout)
    except TimeoutError as e:
        raise Unavailable(f"'{name}': attach timed out after {timeout:g}s") from e
    try:
        surface = await _finish_attach(name, entry, auth, pinned_missing, backend, limiters)
    except BaseException:
        # BaseException: a cancel (shutdown during a retry attach) drops the
        # backend just as surely as a refusal does.
        await close_backend(backend)
        raise
    if backends is not None:
        backends[name] = backend
    return surface


async def _finish_attach(
    name, entry, auth, pinned_missing, backend, limiters: dict | None = None
) -> FastMCP:
    """Everything `_attach_one` does after the backend is built."""
    from .costing import instructions_line, surface_cost
    from .identity import policy_from_entry
    from .plugins import PLUGINS, resolve_pinned

    plugin = PLUGINS.get(entry.plugin)
    # A pin the backend does not serve is a broken published tool, but NOT an
    # attach failure: every other pin still works, and refusing the surface
    # would turn one vanished tool into a whole-surface outage. It used to be
    # silent until someone ran `health --deep`; now it is a WARNING here and a
    # `pinned_missing` entry on /healthz. Best-effort, like costing below — a
    # malformed descriptor must not fail the attach over a diagnostic.
    try:
        missing = missing_pins(backend, resolve_pinned(entry, plugin))
    except Exception:
        logger.warning("pin check failed for surface %r", name, exc_info=True)
        missing = []
    if missing:
        logger.warning(
            "surface %r pins tool(s) its backend does not serve: %s; they are "
            "published but every call to them will fail",
            name,
            ", ".join(missing),
        )
    if pinned_missing is not None:
        if missing:
            pinned_missing[name] = missing
        else:
            pinned_missing.pop(name, None)
    policy = policy_from_entry(entry, plugin.spec) if plugin else None
    # The stdio rule restated for inproc: a surface configured per-user whose
    # in-process source cannot put the identity on its upstream request would
    # silently call as the deployment. Refused, never served.
    if (
        policy is not None
        and policy.mode
        and getattr(backend.executor, "identity_aware", None) is False
    ):
        raise UsageError(
            f"'{name}': declares identity mode '{policy.mode}', but its inproc "
            f"source cannot apply one — build its HTTP client with "
            f"`identity_client` (beherouter.plugin_api)"
        )
    from .gates import gates_from_entry, unserved_names

    unserved = unserved_names(entry, {d.name for d in backend.descriptors})
    if unserved:
        # Like a missing pin: a WARNING, never an attach failure. The gate has
        # nothing to apply to, which is a typo or a backend that dropped a tool.
        logger.warning(
            "surface %r gates or exempts tool(s) its backend does not serve: %s",
            name,
            ", ".join(unserved),
        )
    from .killswitch import stage_for

    stage = stage_for(name)
    surface = build_surface(
        backend,
        auth=auth,
        policy=policy,
        call_timeout_s=call_timeout_s(entry),
        gates=gates_from_entry(entry, limiters),
        stages=(stage,) if stage else (),
    )
    try:
        surface.instructions = instructions_line(await surface_cost(surface, backend), name)
    except Exception:
        logger.warning(
            "context-cost computation failed for surface %r; attaching "
            "without cost instructions",
            name,
            exc_info=True,
        )
    return surface


def _log_attach_failure(name: str, exc: Exception) -> None:
    """ERROR, with the traceback. A config fault says it will not be retried,
    so an operator reading the log knows waiting will not help."""
    if is_config_fault(exc):
        logger.error(
            "surface %r has a configuration fault; serving it as unavailable and "
            "NOT retrying — fix the registry, then reload or restart the gateway",
            name,
            exc_info=exc,
        )
    else:
        logger.error(
            "surface %r failed to attach; serving it as unavailable", name, exc_info=exc
        )


async def build_surfaces(
    registry: dict[str, RegistryEntry],
    auth: object | None = None,
    *,
    failed: dict[str, str] | None = None,
    config_faults: set[str] | None = None,
    pinned_missing: dict[str, list[str]] | None = None,
    backends: dict | None = None,
    limiters: dict | None = None,
) -> dict[str, FastMCP]:
    """One FastMCP surface per attached backend, attached CONCURRENTLY.

    Concurrent because attach is network I/O bounded per surface by
    BEHEROUTER_ATTACH_TIMEOUT_S: serially, boot cost the SUM of every backend's
    latency (worst case N × 30 s of no /healthz); concurrently it costs the
    slowest one. The returned dict is still in registry order.

    With `failed=None` (the CLI, `health`, most tests) an attach failure raises.
    Every attach runs to completion first and the error raised is the first in
    REGISTRY order, not the first to finish, so the same broken registry
    reports the same fault on every run. The gateway passes a dict: a surface
    that fails to attach is then recorded there as `{name: "Type: message"}`
    and skipped, so one dead backend no longer takes every other surface and
    /healthz down with it; a failure `is_config_fault` also lands in
    `config_faults`. `pinned_missing`, when given, collects
    `{name: [pins the backend does not serve]}`. `preflight` runs first either
    way, so a registry mistake still refuses boot.

    `backends`, when given, collects `{name: Backend}` for every attached
    surface: the caller owns them and closes them (`close_backend`) when it
    drops the surfaces. When this function raises, it closes the backends of
    the surfaces that did attach itself — nobody else could. `limiters`, when
    given, is the gateway's `{surface: RateLimiter}`, passed to every attach.
    """
    preflight(registry)
    names = list(registry)
    built: dict = {}
    results = await asyncio.gather(
        *(
            _attach_one(n, registry[n], auth, pinned_missing, built, limiters)
            for n in names
        ),
        return_exceptions=True,
    )
    surfaces: dict[str, FastMCP] = {}
    for name, result in zip(names, results, strict=True):
        if not isinstance(result, BaseException):
            surfaces[name] = result
            continue
        # Cancellation and interpreter exits are never "a surface failed".
        if failed is None or not isinstance(result, Exception):
            await _close_all(built.values())
            raise result
        _log_attach_failure(name, result)
        failed[name] = f"{type(result).__name__}: {result}"
        if config_faults is not None and is_config_fault(result):
            config_faults.add(name)
    if backends is not None:
        backends.update(built)
    return surfaces


def check_registry(registry: dict[str, RegistryEntry]) -> None:
    """Everything that refuses a registry before any attach: `preflight` plus the
    auth-mode rules that depend on the registry. Shared by boot and reload, so a
    reload refuses exactly what boot would."""
    # A gateway that cannot verify a user cannot require one. Refused BEFORE
    # any attach: an entry-level mistake should fail on the configuration, not
    # after a backend has been connected.
    from .identity import gates_on_caller, role_gated

    gated = sorted(name for name, entry in registry.items() if role_gated(entry))
    if auth_mode() == "shared":
        per_user = sorted(
            name for name, entry in registry.items() if gates_on_caller(entry)
        )
        if per_user:
            raise UsageError(
                f"{AUTH_MODE_VAR} is 'shared' but surface(s) "
                f"{per_user} require a verified user; "
                f"set it to 'oidc' or 'both'"
            )
    # A role gate with nowhere to read roles from can only fail closed on every
    # call, so it fails at boot instead — where an operator is looking.
    if gated and not roles_claim():
        raise UsageError(
            f"surface(s) {gated} gate on roles but {OIDC_ROLES_CLAIM_VAR} is "
            f"unset; set it to the dotted path of the claim your IdP puts roles "
            f"in (e.g. 'realm_access.roles')"
        )
    preflight(registry)


async def build_gateway_app(
    registry: dict[str, RegistryEntry] | Path | str,
    *,
    strict_auth: bool = True,
    retry_initial_s: float = 5.0,
    retry_max_s: float = 300.0,
):
    """Build the ASGI app serving each surface's http_app() at /<tool>/mcp.

    Every surface is a slot in the runtime's `SurfaceTable` (`runtime.py`), so
    it can be retried, and later swapped, without rebuilding the app.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    from . import killswitch
    from .admin import AdminAuth, admin_routes, check_admin_config
    from .audit import AuditSink
    from .reload import drain_s, watch_s, watch_stamp
    from .runtime import GatewayRuntime

    path: Path | None = None
    registry_stamp: tuple | None = None
    if not isinstance(registry, dict):
        path = Path(registry)
        # Stamped BEFORE the read: an edit landing between the two is then
        # seen by the watch instead of being mistaken for the baseline.
        registry_stamp = watch_stamp([path])
        registry = load_registry(path)
    # preflight runs here and AGAIN in build_surfaces (via boot): intentional,
    # build_surfaces stays self-checking for its other callers, and it is offline.
    check_registry(registry)
    admin = AdminAuth.from_env()
    check_admin_config(admin)  # an admin token equal to the shared one refuses boot

    ks = killswitch.configured()
    if ks is not None:
        ks.state()  # a malformed file with no last-good state refuses boot (§5.2)

    watch_s()  # a bad BEHEROUTER_REGISTRY_WATCH_S refuses boot, not the first tick
    auth = ObservedVerifier(build_verifier(strict=strict_auth))
    runtime = GatewayRuntime(
        registry,
        auth=auth,
        path=path,
        retry_initial_s=retry_initial_s,
        retry_max_s=retry_max_s,
        drain_s=drain_s(),
        registry_stamp=registry_stamp,
    )
    await runtime.boot()

    async def healthz(_request):
        """Unauthenticated liveness (CONVENTIONS §Health).

        Deliberately does NOT probe the backends. This answers "is the gateway
        up and did its registry load", which is what an external monitor needs;
        a fan-out to every backend would make the probe as slow and as flaky as
        the slowest one, and turn one backend's outage into a gateway alarm.
        Per-backend liveness is a separate contract (HARNESS-PLAN Phase 2 §8).

        Exposes only surface names — already public in the URL path — so
        leaving it unauthenticated leaks nothing.

        A surface that failed to attach makes the status `degraded` — still
        HTTP 200, because the gateway itself is up and every other surface is
        serving; alert on the field. Only names are listed.

        Two further keys, each present only when non-empty, so a clean gateway
        still answers exactly `{"status": "ok", "surfaces": [...]}`:
        `needs_config_change` — the subset of `failed` the gateway has stopped
        retrying because the fault is in configuration; `pinned_missing` —
        `{surface: [pins]}` for attached surfaces whose backend does not serve a
        pinned tool. The latter does NOT make `status` degraded: `status` means
        "every surface attached", which external probes match on, and a surface
        with one dead pin is attached and serving its others.
        `health --deep` is the check that fails on it. Pin names are not
        secret — they are the plugin's or the registry's own vocabulary, not
        anything a backend or a caller supplied.

        Four more, also present only when non-empty (spec §6.2), none of which
        makes `status` degraded: `reload_failed` — surfaces still serving their
        pre-reload app because the new entry failed to attach; `last_reload` —
        `{"status": "failed", "at": ...}` while the last reload failed at lint;
        `disabled` — the kill switch's quarantined surface names, or `["*"]`
        while `all` is set (a deliberate stop is not a fault: alert on this
        key); `killswitch` — `"stale"` while a malformed state file is ignored.
        No blocked subject ever appears here.
        """
        return JSONResponse(runtime.health_body())

    async def metrics_route(_request):
        """Unauthenticated, like /healthz: labelled by surface, tool, outcome
        and reason, never by caller or token. Behind the reference Caddy vhost it is not
        reachable from outside at all (default-deny); on Kubernetes, restrict
        it the way you restrict /healthz if that matters to you.
        """
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # start() installs every boot-attached app before `yield`, so the first
        # request already finds each one's session manager running.
        try:
            await runtime.start()
            yield
        finally:
            await runtime.shutdown()

    # /healthz, /metrics and /admin first: a surface may not be named any of
    # them, but explicit Routes ahead of the Mounts make that collision
    # impossible rather than merely unlikely. No admin credential configured
    # means no /admin route at all (spec §4.2): an upgrade adds no network surface.
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/metrics", metrics_route, methods=["GET"]),
        *(admin_routes(runtime, admin, auth, AuditSink.from_env()) if admin else []),
    ]
    # Around every route, so each surface's verifier reports into the slot it
    # opens and an expired token's 401 says so on the way out. The table is the
    # label set, so a surface added by a reload is a label at once.
    app = Starlette(
        routes=routes,
        middleware=[Middleware(RejectionMiddleware, surfaces=runtime.table)],
        lifespan=lifespan,
    )
    # The table publishes onto the app's own router (fixed routes + one Mount
    # per slot), rather than living behind a nested Mount(""): the surfaces stay
    # top-level routes, exactly as before, and an unknown path is an RFC 9457 404.
    runtime.table.bind(app.router, routes)
    app.state.runtime = runtime  # tests and the admin API reach it here
    return app


def serve(
    registry_path: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Run the gateway (blocking). Refuses to start without a shared token.

    An EMPTY registry is served, not refused: a gateway fronting nothing is a
    valid operating state (parked between backends), and it must keep answering
    /healthz so that emptying the registry on purpose does not read to the
    monitor as an outage. Missing auth is the opposite case and still fails at
    boot — see `SharedTokenVerifier.from_env(strict=True)`.
    """
    import asyncio

    import uvicorn

    from .logsetup import configure

    configure()
    # The path, not a loaded dict: the runtime keeps it, to reload from.
    app = asyncio.run(build_gateway_app(Path(registry_path)))
    uvicorn.run(app, host=host, port=port, log_config=None)

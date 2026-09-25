"""Assemble all surfaces from the registry and serve them under /<tool>/mcp behind auth."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from fastmcp import FastMCP

from .auth import (
    AUTH_MODE_VAR,
    OIDC_ROLES_CLAIM_VAR,
    ObservedVerifier,
    RejectionMiddleware,
    auth_mode,
    build_verifier,
    metrics_text,
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


def preflight(registry: dict[str, RegistryEntry]) -> None:
    """Refuse to boot on what `registry-lint` can see. No I/O.

    These are operator mistakes with a local fix — an unknown plugin, a bad
    config type, an unset `${VAR}` — and a gateway that booted around them
    would hide them behind a degraded surface. Everything else a surface can
    fail on is found only by attaching, and is isolated to that surface.
    """
    attach_timeout_s()
    for name, entry in registry.items():
        validate_entry(entry)
        if entry.env:
            expand(name, entry.env)


async def load_backend(entry: RegistryEntry):
    """Resolve a registry entry through its plugin.

    Only the final `build` may perform I/O; everything before it is validation
    against inert data.
    """
    from .plugins import get, resolve_aliases, resolve_pinned
    from .plugins.spec import PluginContext
    from .plugins.validate import validate_config

    validate_entry(entry)
    plugin = get(entry.plugin)
    # A probe and its arguments are ONE unit — see health.check_entry for why
    # falling back field-by-field calls the right tool with the wrong arguments.
    probe, probe_args = (
        (entry.probe, entry.probe_args)
        if entry.probe
        else (plugin.spec.probe, plugin.spec.probe_args)
    )
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


async def _attach_one(name: str, entry: RegistryEntry, auth: object | None) -> FastMCP:
    """Attach one surface: backend, policy, surface, cost instructions.

    `instructions` is set HERE rather than in build_surface because computing
    the cost needs FastMCP.list_tools(), which is async, and build_surface is
    not. A host reads instructions at `initialize`, so it learns what the
    surface costs without spending a tool call.

    Computing the cost is NOT allowed to fail the attach: `naive_tokens`
    re-registers every descriptor a backend has through the signature-synthesis
    path, so a schema edge case in one unpinned tool would otherwise fail the
    whole surface. It degrades to a plain, cost-free `instructions` (logged).
    """
    from .costing import instructions_line, surface_cost
    from .identity import policy_from_entry
    from .plugins import PLUGINS

    timeout = attach_timeout_s()
    try:
        backend = await asyncio.wait_for(load_backend(entry), timeout)
    except TimeoutError as e:
        raise Unavailable(f"'{name}': attach timed out after {timeout:g}s") from e
    plugin = PLUGINS.get(entry.plugin)
    policy = policy_from_entry(entry, plugin.spec) if plugin else None
    surface = build_surface(backend, auth=auth, policy=policy)
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


async def build_surfaces(
    registry: dict[str, RegistryEntry],
    auth: object | None = None,
    *,
    failed: dict[str, str] | None = None,
) -> dict[str, FastMCP]:
    """One FastMCP surface per attached backend.

    With `failed=None` (the CLI, `health`, most tests) the first attach
    failure raises, as it always has. The gateway passes a dict: a surface
    that fails to attach is then recorded there as `{name: "Type: message"}`
    and skipped, so one dead backend no longer takes every other surface and
    /healthz down with it. `preflight` runs first either way, so a registry
    mistake still refuses boot.
    """
    preflight(registry)
    surfaces: dict[str, FastMCP] = {}
    for name, entry in registry.items():
        if failed is None:
            surfaces[name] = await _attach_one(name, entry, auth)
            continue
        try:
            surfaces[name] = await _attach_one(name, entry, auth)
        except Exception as e:
            logger.exception(
                "surface %r failed to attach; serving it as unavailable", name
            )
            failed[name] = f"{type(e).__name__}: {e}"
    return surfaces


def _combined_lifespan(sub_apps: list):
    """Run every mounted surface's own lifespan alongside the parent app's.

    Each `http_app()` carries an MCP session manager that is started by its
    lifespan. Starlette does NOT run the lifespan of sub-apps mounted via
    `Mount`, so without this every request would fail with "Task group is not
    initialized".
    """

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for sub in sub_apps:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    return lifespan


async def build_gateway_app(
    registry: dict[str, RegistryEntry] | Path | str,
    *,
    strict_auth: bool = True,
):
    """Build the ASGI app mounting each surface's http_app() at /<tool>/mcp."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    if not isinstance(registry, dict):
        registry = load_registry(Path(registry))

    # A gateway that cannot verify a user cannot require one. Refused BEFORE
    # any attach: an entry-level mistake should fail on the configuration, not
    # after a backend has been connected.
    from .identity import gates_on_caller

    gated = sorted(
        name
        for name, entry in registry.items()
        if (entry.authz or {}).get("require_roles")
    )
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

    auth = ObservedVerifier(build_verifier(strict=strict_auth))
    surfaces = await build_surfaces(registry, auth=auth)

    sub_apps = {name: s.http_app(path="/mcp") for name, s in surfaces.items()}

    async def healthz(_request):
        """Unauthenticated liveness (CONVENTIONS §Health).

        Deliberately does NOT probe the backends. This answers "is the gateway
        up and did its registry load", which is what an external monitor needs;
        a fan-out to every backend would make the probe as slow and as flaky as
        the slowest one, and turn one backend's outage into a gateway alarm.
        Per-backend liveness is a separate contract (HARNESS-PLAN Phase 2 §8).

        Exposes only surface names — already public in the URL path — so
        leaving it unauthenticated leaks nothing.
        """
        return JSONResponse({"status": "ok", "surfaces": sorted(sub_apps)})

    async def metrics(_request):
        """Unauthenticated, like /healthz: counters only, labelled by reason,
        never by caller or token. Behind the reference Caddy vhost it is not
        reachable from outside at all (default-deny); on Kubernetes, restrict
        it the way you restrict /healthz if that matters to you.
        """
        return PlainTextResponse(
            metrics_text(), media_type="text/plain; version=0.0.4"
        )

    # /healthz and /metrics first: a surface may not be named either, but an
    # explicit Route ahead of the Mounts makes that collision impossible rather
    # than merely unlikely.
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        *[Mount(f"/{name}", app=app) for name, app in sub_apps.items()],
    ]
    # Around every route, so each surface's verifier reports into the slot it
    # opens and an expired token's 401 says so on the way out.
    return Starlette(
        routes=routes,
        middleware=[Middleware(RejectionMiddleware)],
        lifespan=_combined_lifespan(list(sub_apps.values())),
    )


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

    registry = load_registry(Path(registry_path))
    app = asyncio.run(build_gateway_app(registry))
    uvicorn.run(app, host=host, port=port)

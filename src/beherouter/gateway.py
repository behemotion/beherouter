"""Assemble all surfaces from the registry and serve them under /<tool>/mcp behind auth."""

import contextlib
import logging
from pathlib import Path

from fastmcp import FastMCP

from .auth import AUTH_MODE_VAR, auth_mode, build_verifier
from .envexpand import expand
from .errors import UsageError
from .registry import RegistryEntry, load_registry, validate_entry
from .surface import build_surface

logger = logging.getLogger(__name__)

DEFAULT_PORT = 47100
DEFAULT_HOST = "0.0.0.0"


async def load_backend(entry: RegistryEntry):
    """Resolve a registry entry through its plugin.

    Only the final `build` may perform I/O; everything before it is validation
    against inert data.
    """
    from .plugins import get, resolve_pinned
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
    return backend


async def build_surfaces(
    registry: dict[str, RegistryEntry], auth: object | None = None
) -> dict[str, FastMCP]:
    """One FastMCP surface per attached backend.

    `instructions` is set HERE rather than in build_surface because computing
    the cost needs FastMCP.list_tools(), which is async, and build_surface is
    not. A host reads instructions at `initialize`, so it learns what the
    surface costs without spending a tool call.

    Computing the cost is NOT allowed to fail the attach. `naive_tokens`
    re-registers every descriptor a backend has -- pinned or not -- through the
    same signature-synthesis path `context_cost` otherwise only exercises
    lazily, on an explicit tool call where a failure is an isolated error. Here
    it runs unconditionally for every attached backend, so a schema edge case
    in even one unpinned tool would otherwise turn into a failed attach --
    and AGENTS.md is explicit that an attach failure crash-loops the whole
    gateway, taking every other surface and /healthz down with it. So one
    surface's costing failure degrades to a plain, cost-free `instructions`
    (logged), rather than raising -- the same "report, don't take the whole
    process down" instinct as `health.check_entry` and `gateway.healthz`.
    """
    from .costing import instructions_line, surface_cost
    from .identity import policy_from_entry
    from .plugins import PLUGINS

    surfaces: dict[str, FastMCP] = {}
    for name, entry in registry.items():
        backend = await load_backend(entry)
        plugin = PLUGINS.get(entry.plugin)
        policy = policy_from_entry(entry, plugin.spec) if plugin else None
        surface = build_surface(backend, auth=auth, policy=policy)
        try:
            surface.instructions = instructions_line(
                await surface_cost(surface, backend), name
            )
        except Exception:
            logger.warning(
                "context-cost computation failed for surface %r; attaching "
                "without cost instructions",
                name,
                exc_info=True,
            )
        surfaces[name] = surface
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
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    if not isinstance(registry, dict):
        registry = load_registry(Path(registry))

    # A gateway that cannot verify a user cannot require one. Refused BEFORE
    # any attach: an entry-level mistake should fail on the configuration, not
    # after a backend has been connected.
    if auth_mode() == "shared":
        per_user = sorted(
            name
            for name, entry in registry.items()
            if (entry.identity or {}).get("mode") not in (None, "", "none")
        )
        if per_user:
            raise UsageError(
                f"{AUTH_MODE_VAR} is 'shared' but surface(s) {per_user} require "
                f"a per-user identity; set it to 'oidc' or 'both'"
            )

    auth = build_verifier(strict=strict_auth)
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

    # /healthz first: a surface may not be named "healthz", but an explicit
    # Route ahead of the Mounts makes that collision impossible rather than
    # merely unlikely.
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        *[Mount(f"/{name}", app=app) for name, app in sub_apps.items()],
    ]
    return Starlette(routes=routes, lifespan=_combined_lifespan(list(sub_apps.values())))


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

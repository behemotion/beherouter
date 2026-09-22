"""beherouter CLI — built on beheaxi (dogfoods the standard).

`describe` is auto-registered by BeheaxiApp and deliberately NOT declared here;
re-registering it would corrupt the command tree.
"""

import asyncio
import logging
import os
from pathlib import Path

from beheaxi import BeheaxiApp

from ..clientconfig import render as render_client_config
from ..clientconfig import resolve_public_url
from ..errors import NotFound, Unavailable
from ..gateway import DEFAULT_HOST, DEFAULT_PORT, load_backend
from ..health import deep_health, failed
from ..indexing import build_index
from ..registry import RegistryEntry, load_registry, save_registry, validate_entry

app = BeheaxiApp(
    name="beherouter",
    version="0.1.0",
    summary="Multi-service MCP gateway for the BEHEMOTION harness.",
)

logger = logging.getLogger(__name__)


def _registry_path() -> Path:
    return Path(
        os.environ.get("BEHEROUTER_REGISTRY", Path.home() / ".config/beherouter/registry.toml")
    )


def _load(entry: RegistryEntry):
    return asyncio.run(load_backend(entry))


@app.command(pinned=True, mutating=False)
def surfaces() -> None:
    """List attached surfaces and the plugin behind each."""
    reg = load_registry(_registry_path())
    app.emit({"surfaces": [{"name": n, "plugin": e.plugin} for n, e in reg.items()]})


@app.command(pinned=False, mutating=True)
def attach(tool: str, plugin: str, config: str = "") -> None:
    """Attach a surface: `attach <name> <plugin> [--config k=v,k=v]`."""
    parsed: dict = {}
    for pair in (p for p in config.split(",") if p):
        k, _, v = pair.partition("=")
        parsed[k.strip()] = v.strip()
    entry = RegistryEntry(name=tool, plugin=plugin, config=parsed or None)
    validate_entry(entry)
    # Load once before saving: a backend that cannot be reached is not attached.
    # Raises UsageError/Conflict/Unavailable -> the matching exit code.
    backend = _load(entry)
    reg = load_registry(_registry_path())
    reg[tool] = entry
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_registry(path, reg)
    app.emit({"attached": tool, "plugin": plugin, "tools": len(backend.descriptors)})


@app.command(pinned=False, mutating=True)
def detach(tool: str) -> None:
    """Detach a backend (NotFound if absent)."""
    reg = load_registry(_registry_path())
    if tool not in reg:
        raise NotFound(f"no attached tool '{tool}'")
    del reg[tool]
    save_registry(_registry_path(), reg)
    app.emit({"detached": tool})


@app.command(pinned=True, mutating=False)
def health(deep: bool = False) -> None:
    """Report gateway/registry health; --deep probes each backend's credential."""
    reg = load_registry(_registry_path())
    if not deep:
        # Registry counts only, and deliberately so: the default must not fan out
        # to the backends, or it becomes as slow and as flaky as the slowest one.
        app.emit({"ok": True, "count": len(reg)})
        return
    records = asyncio.run(deep_health(reg))
    bad = failed(records)
    # Records first: the verdict travels in the exit code, but the operator needs
    # to be told WHICH backend died, so the detail must reach stdout either way.
    app.emit({"ok": not bad, "count": len(reg), "backends": records})
    if bad:
        raise Unavailable(f"backend health check failed: {', '.join(bad)}")


@app.command(pinned=True, mutating=False)
def registry_lint(path: str = "") -> None:
    """Validate a registry file offline: no network, no attach.

    An attach failure crash-loops the gateway and takes /healthz with it, so the
    only other way to find out whether a registry edit is valid is to deploy it.
    This runs the whole validation path — plugin lookup, config schema, the
    plugin's own validators, and ${VAR} resolution — against inert data.
    """
    from .. import auth
    from ..envexpand import expand
    from ..errors import UsageError
    from ..identity import DEFAULT_MAP_VAR, SecretMap

    target = Path(path) if path else _registry_path()
    reg = load_registry(target)
    for entry in reg.values():
        validate_entry(entry)
        if entry.env:
            # Checked against the LOCAL environment, so this catches the
            # entry-before-secret ordering mistake only where the variables
            # exist. The ordering rule — entry and secret ship in the same
            # playbook run — is the real guard; lint is the backstop.
            expand(entry.name, entry.env)
        identity = entry.identity or {}
        mode = identity.get("mode")
        gate = (entry.authz or {}).get("require_roles")
        if (mode and mode != "none") or gate:
            # ⚠️ Both rules below are checked ONLY where the gateway's own
            # environment is visible. Lint runs on a workstation and in an init
            # container, and defaulting to 'shared' there would fail a valid
            # registry — so boot is the authority and this is the early
            # warning, the reverse of every other rule in this function. The
            # chart renders BEHEROUTER_AUTH_MODE into the hook Job for exactly
            # this reason: an omitted variable silently skips both.
            #
            # The mode comes FIRST, matching `gateway.build_gateway_app`: on a
            # shared-mode gateway the mode is the cause and a missing claim
            # path is a symptom, and reporting the symptom sends an operator to
            # configure a claim they do not need yet.
            if os.environ.get(auth.AUTH_MODE_VAR) and auth.auth_mode() == "shared":
                raise UsageError(
                    f"'{entry.name}' requires a verified user but "
                    f"{auth.AUTH_MODE_VAR} is 'shared'; set it to 'oidc' or 'both'"
                )
            if (
                gate
                and os.environ.get(auth.AUTH_MODE_VAR)
                and not os.environ.get(auth.OIDC_ROLES_CLAIM_VAR)
            ):
                raise UsageError(
                    f"'{entry.name}' gates on roles but "
                    f"{auth.OIDC_ROLES_CLAIM_VAR} is unset; set it to the "
                    f"dotted path of the claim your IdP puts roles in"
                )
            if mode == "lookup":
                path_ = identity.get("path") or os.environ.get(DEFAULT_MAP_VAR)
                # Checked only where the mount exists, exactly like ${VAR}
                # expansion above: lint is the backstop, not the guard.
                if path_ and Path(path_).exists():
                    status = SecretMap(path_).status()
                    if status["state"] != "ok":
                        raise UsageError(
                            f"'{entry.name}': identity map '{path_}' is "
                            f"{status['state']}"
                        )
                elif path_:
                    raise UsageError(
                        f"'{entry.name}': identity map '{path_}' does not exist"
                    )
    app.emit({"ok": True, "path": str(target), "surfaces": sorted(reg)})


@app.command(pinned=True, mutating=False)
def search(tool: str, query: str) -> None:
    """BM25-search a backend's tools; returns ranked flat names."""
    entry = load_registry(_registry_path()).get(tool)
    if entry is None:
        raise NotFound(f"no attached tool '{tool}'")
    backend = _load(entry)
    by_name = {d.name: d for d in backend.descriptors}
    hits = [
        {
            "name": n,
            "summary": by_name[n].summary,
            "pinned": by_name[n].pinned,
            "mutating": by_name[n].mutating,
        }
        for n in build_index(backend.descriptors).search(query)
    ]
    app.emit({"hits": hits})


@app.command(pinned=True, mutating=False)
def context_cost(surface: str = "", context_window: int = 0) -> None:
    """Report each surface's context cost: `context-cost [--surface S] [--context-window N]`.

    Attaches each backend, because the catalogue only exists after connecting —
    for `http`, `stdio` and `cli` backings there is nothing to measure until
    then. Only `native` plugins could be costed inertly, and special-casing
    them would make one command mean two different things.

    Reports rather than raises, per `health --deep`: one dead backend must not
    hide the cost of the others.
    """
    from ..costing import as_payload, surface_cost
    from ..surface import build_surface

    reg = load_registry(_registry_path())
    if surface:
        if surface not in reg:
            raise NotFound(f"no attached tool '{surface}'")
        reg = {surface: reg[surface]}
    window = context_window or None

    async def _measure() -> list[dict]:
        records = []
        for name, entry in reg.items():
            try:
                backend = await load_backend(entry)
                cost = await surface_cost(build_surface(backend), backend)
            except Exception as e:
                logger.warning(
                    "context-cost measurement failed for surface %r", name, exc_info=True
                )
                records.append({"name": name, "error": str(e)})
                continue
            records.append({"name": name, **as_payload(cost, window)})
        return records

    records = asyncio.run(_measure())
    bad = [r["name"] for r in records if "error" in r]
    app.emit({"ok": not bad, "surfaces": records})
    if bad:
        raise Unavailable(f"could not measure: {', '.join(bad)}")


@app.command(pinned=True, mutating=False)
def plugins() -> None:
    """List available plugins."""
    from ..plugins import PLUGINS

    app.emit(
        {
            "plugins": [
                {"name": n, "backing": p.spec.backing, "summary": p.spec.summary}
                for n, p in sorted(PLUGINS.items())
            ]
        }
    )


@app.command(pinned=True, mutating=False)
def plugin_config(surface: str, plugin: str) -> None:
    """Emit the registry, Caddy and env fragments: `plugin-config <surface> <plugin>`."""
    from ..pluginconfig import render

    app.emit(render(surface, plugin))


@app.command(pinned=True, mutating=False)
def client_config(agent: str, base_url: str | None = None) -> None:
    """Emit paste-ready MCP config for a client (librechat|claude-code|pi|opencode|hermes)."""
    reg = load_registry(_registry_path())
    app.emit(render_client_config(agent, list(reg), resolve_public_url(base_url)))


@app.command(pinned=False, mutating=False)
def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the gateway (blocking); requires BEHEROUTER_GATEWAY_TOKEN."""
    from ..gateway import serve as _serve

    _serve(_registry_path(), host=host, port=port)


def main() -> None:
    raise SystemExit(app.main())


if __name__ == "__main__":
    main()

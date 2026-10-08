"""beherouter CLI — built on beheaxi (dogfoods the standard).

`describe` is auto-registered by BeheaxiApp and deliberately NOT declared here;
re-registering it would corrupt the command tree.
"""

import asyncio
import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from beheaxi import BeheaxiApp

from ..clientconfig import render as render_client_config
from ..clientconfig import resolve_public_url
from ..errors import NotFound, Unavailable
from ..gateway import DEFAULT_HOST, DEFAULT_PORT, load_backend
from ..health import deep_health, failed
from ..indexing import build_index, search_hits
from ..registry import RegistryEntry, load_registry, save_registry, validate_entry


def _version() -> str:
    """The installed distribution's version (CONVENTIONS §7: never hardcoded).

    A literal here shipped as `0.1.0` through three releases, so every manifest
    a sibling read from `describe --json` named a version that never existed.
    """
    try:
        return _dist_version("beherouter")
    except PackageNotFoundError:  # a bare source tree on sys.path, not installed
        return "0+unknown"


app = BeheaxiApp(
    name="beherouter",
    version=_version(),
    summary="Multi-service MCP gateway for the BEHEMOTION harness.",
)

logger = logging.getLogger(__name__)


def _registry_path() -> Path:
    return Path(
        os.environ.get("BEHEROUTER_REGISTRY", Path.home() / ".config/beherouter/registry.toml")
    )


def _load(entry: RegistryEntry):
    return asyncio.run(load_backend(entry))


def _missing_stdio_command(entry: RegistryEntry) -> str | None:
    """The executable a stdio or cli entry's `cmd` names, when it is not on PATH here.

    Read from the plugin's `cmd` config key — the convention every stdio and
    cli plugin here follows. No `cmd` key, or another backing, means nothing
    to check.
    """
    import shlex
    import shutil

    from ..plugins import get
    from ..plugins.validate import validate_config

    plugin = get(entry.plugin)
    if plugin.spec.backing not in ("stdio", "cli"):
        return None
    cmd = validate_config(entry.name, plugin.spec, entry.config).get("cmd")
    argv = shlex.split(cmd) if isinstance(cmd, str) else []
    if argv and shutil.which(argv[0]) is None:
        return argv[0]
    return None


@app.command(pinned=True, mutating=False)
def surfaces() -> None:
    """List attached surfaces and the plugin behind each."""
    reg = load_registry(_registry_path())
    app.emit({"surfaces": [{"name": n, "plugin": e.plugin} for n, e in reg.items()]})


_TRUE_FALSE = {"true": True, "false": False}


def parse_config(spec, text: str) -> dict:
    """`k=v,k=v` → a config table typed per the plugin's `ConfigField`s.

    Syntax: pairs separated by `,`; whitespace around keys and values is
    stripped. `int`/`float` parse as Python numbers; `bool` is `true` or
    `false` (any case) and nothing else, matching TOML's two booleans; a
    `list` field takes ONE PAIR PER ITEM (`include=a,include=b`), because `,`
    already separates pairs and a second escape character would be one more
    thing to get wrong. A key the plugin does not declare passes through as a
    string, so `validate_config` refuses it with the list of allowed keys.

    Before this, every value arrived as a string and the plugin's own type
    check refused it — no plugin with a non-string key could be attached from
    the CLI at all.
    """
    from ..errors import UsageError

    fields = {f.name: f for f in spec.config}
    out: dict = {}
    for pair in (p for p in text.split(",") if p.strip()):
        key, sep, raw = pair.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key:
            raise UsageError(f"--config: expected key=value, got {pair.strip()!r}")
        field = fields.get(key)
        if field is None:
            out[key] = raw
            continue
        if field.type is list:
            out.setdefault(key, []).append(raw)
            continue
        if key in out:
            raise UsageError(
                f"--config: '{key}' given more than once; only a list field repeats"
            )
        value: Any
        try:
            if field.type is bool:
                value = _TRUE_FALSE[raw.lower()]
            elif field.type in (int, float):
                value = field.type(raw)
            else:
                value = raw
        except (KeyError, ValueError):
            expected = "bool (true or false)" if field.type is bool else field.type.__name__
            raise UsageError(f"--config: '{key}' must be {expected}, got {raw!r}") from None
        out[key] = value
    return out


@app.command(pinned=False, mutating=True)
def attach(tool: str, plugin: str, config: str = "") -> None:
    """Attach a surface: `attach <name> <plugin> [--config k=v,k=v]` (list: repeat k)."""
    from ..plugins import get

    parsed = parse_config(get(plugin).spec, config)
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


def _read_bearer(path: str) -> str:
    """A user token from a file (`-` = stdin), never from argv: argv is visible
    to every process on the host, and to `ps` in every sidecar of the pod."""
    import sys

    from ..errors import UsageError

    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise UsageError(f"--bearer-file {path!r} holds no token")
    return token


@app.command(pinned=True, mutating=False)
def health(
    deep: bool = False, bearer_file: str = "", surface: str = "", textfile: str = ""
) -> None:
    """Report health; --deep probes each backend, --bearer-file F also probes as that user.

    `--textfile PATH` (with --deep) also writes the verdict as a node_exporter
    textfile-collector file, atomically — the scheduled-check contract in
    `healthmetrics.py` and contrib/health-textfile/.
    """
    from ..errors import UsageError

    reg = load_registry(_registry_path())
    if surface:
        if surface not in reg:
            raise NotFound(f"no attached tool '{surface}'")
        reg = {surface: reg[surface]}
    if bearer_file and not deep:
        raise UsageError("--bearer-file probes a backend, so it needs --deep")
    if textfile and not deep:
        raise UsageError("--textfile records a deep probe, so it needs --deep")
    if textfile and not Path(textfile).parent.is_dir():
        # Checked BEFORE the sweep, and never created: the collector owns its
        # directory, and a typo must not quietly make one nothing scrapes.
        raise UsageError(f"--textfile: directory {str(Path(textfile).parent)!r} does not exist")
    if not deep:
        # Registry counts only, and deliberately so: the default must not fan out
        # to the backends, or it becomes as slow and as flaky as the slowest one.
        app.emit({"ok": True, "count": len(reg)})
        return
    token = _read_bearer(bearer_file) if bearer_file else None
    records = asyncio.run(deep_health(reg, user_token=token))
    bad = failed(records)
    if textfile:
        import time

        from ..healthmetrics import render, write_textfile

        # Written on failure too, BEFORE the exit code is raised: skipping the
        # write on a red sweep would leave the last GREEN file for the
        # collector to keep exporting.
        write_textfile(textfile, render(records, now=time.time(), failed_names=bad))
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

    Emits a `warnings` array beside the verdict for what is legal but a trap:
    warnings never fail the lint, because a gate in front of a gateway that
    would otherwise be dead on arrival must not refuse a registry the gateway
    would happily serve.
    """
    from .. import auth
    from ..envexpand import PLACEHOLDER, expand
    from ..errors import UsageError
    from ..identity import DEFAULT_MAP_VAR, SecretMap, gates_on_caller
    from ..maturity import lint_warning
    from ..pluginconfig import collision_warning
    from ..plugins import ENTRY_POINT_FAILURES
    from ..plugins import get as get_plugin
    from ..plugins.validate import validate_config

    target = Path(path) if path else _registry_path()
    reg = load_registry(target)
    # A WARNING, not a refusal: a broken third-party package the registry
    # does not name costs nothing, and one it does name already fails below as
    # "unknown plugin" — with these entry points named in the message.
    warnings: list[str] = [
        f"plugin entry point '{f['entry_point']}' ({f['value']}) failed to load: "
        f"{f['error']}"
        for f in ENTRY_POINT_FAILURES
    ]
    for entry in reg.values():
        validate_entry(entry)
        lint_plugin = get_plugin(entry.plugin)
        # A WARNING, never a failure: every generic and imported entry is
        # `declared` by construction. It names what would raise the tier.
        maturity_warning = lint_warning(entry.name, entry.plugin, lint_plugin.spec)
        if maturity_warning:
            warnings.append(maturity_warning)
        if lint_plugin.warn is not None:
            config = validate_config(entry.name, lint_plugin.spec, entry.config)
            warnings.extend(f"'{entry.name}': {w}" for w in lint_plugin.warn(config))
        if entry.search_aliases:
            # A WARNING: lint never attaches, so the full catalogue is unknown
            # here -- only the pins and the plugin's own vocabulary are.
            # `health --deep` checks the words against the live catalogue.
            spec = lint_plugin.spec
            known = set(spec.pinned) | set(spec.search_aliases) | set(entry.pinned or [])
            warnings.extend(
                f"'{entry.name}': search_aliases names '{tool}', which plugin "
                f"'{entry.plugin}' neither pins nor has vocabulary for; check "
                f"the name (`health --deep` verifies it against the backend)."
                for tool in sorted(set(entry.search_aliases) - known)
            )
        # ⚠️ WARN, never refuse: deployments (this harness's own included)
        # already use the colliding name, and lint is the gate in front of a
        # gateway that would otherwise be dead on arrival. The rule itself lives
        # in `pluginconfig` beside the naming it is about.
        warnings.extend(
            w
            for w in (
                collision_warning(entry.name, credential, value)
                for credential, value in (entry.env or {}).items()
            )
            if w
        )
        stdio_missing = _missing_stdio_command(entry)
        if stdio_missing:
            # A WARNING, because lint also runs on workstations that lack the
            # image's binaries. In the image that will serve (the chart's hook
            # Job) it is exactly the attach failure that would crash-loop the
            # gateway, which refuses it by name at boot.
            warnings.append(
                f"'{entry.name}': command '{stdio_missing}' is not "
                f"installed here. If this is the image that will serve, the "
                f"attach will fail; use an image that ships it or override "
                f"`cmd` in [{entry.name}.config]."
            )
        if entry.env:
            # Checked against the LOCAL environment, so this catches the
            # entry-before-secret ordering mistake only where the variables
            # exist. The ordering rule — entry and secret ship in the same
            # playbook run — is the real guard; lint is the backstop.
            expand(entry.name, entry.env)
        identity = entry.identity or {}
        mode = identity.get("mode")
        gate = (entry.authz or {}).get("require_roles")
        if mode == "exchange":
            # A WARNING, unlike an unset [surface.env] ${VAR}: the exchange
            # secret is resolved per call, so the gateway boots without it and
            # every exchange on this surface then fails. Checked against the
            # LOCAL environment, like the lookup map below; names the variable,
            # never a value.
            found = PLACEHOLDER.match(str(identity.get("client_secret") or ""))
            if found and not os.environ.get(found.group(1)):
                warnings.append(
                    f"'{entry.name}': identity client_secret ${{{found.group(1)}}} "
                    f"is unset here. The gateway boots without it, and every "
                    f"token exchange on this surface fails until it is set."
                )
        if gates_on_caller(entry):
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
    app.emit(
        {
            "ok": True,
            "path": str(target),
            "surfaces": sorted(reg),
            "warnings": warnings,
        }
    )


@app.command(pinned=True, mutating=False)
def search(tool: str, query: str, limit: int = 5) -> None:
    """Search a backend's tools; returns ranked hits, as search_tools does."""
    entry = load_registry(_registry_path()).get(tool)
    if entry is None:
        raise NotFound(f"no attached tool '{tool}'")
    backend = _load(entry)
    hits = search_hits(
        build_index(backend.descriptors, backend.search_aliases),
        {d.name: d for d in backend.descriptors},
        query,
        limit,
        {d.name for d in backend.pinned},
    )
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
            backend = None
            try:
                backend = await load_backend(entry)
                cost = await surface_cost(build_surface(backend), backend)
            except Exception as e:
                logger.warning(
                    "context-cost measurement failed for surface %r", name, exc_info=True
                )
                records.append({"name": name, "error": str(e)})
                continue
            finally:
                # Each built backend is dropped here, so release what its
                # executor holds (a kept-alive subprocess, an HTTP client).
                aclose = getattr(getattr(backend, "executor", None), "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        logger.warning("closing surface %r failed", name, exc_info=True)
            records.append({"name": name, **as_payload(cost, window)})
        return records

    records = asyncio.run(_measure())
    bad = [r["name"] for r in records if "error" in r]
    app.emit({"ok": not bad, "surfaces": records})
    if bad:
        raise Unavailable(f"could not measure: {', '.join(bad)}")


@app.command(pinned=True, mutating=False)
def plugins() -> None:
    """List available plugins, and any out-of-tree entry point that failed to load."""
    from ..maturity import displayed_tier
    from ..plugins import ENTRY_POINT_FAILURES, PLUGINS

    rows = []
    for n, p in sorted(PLUGINS.items()):
        tier, note = displayed_tier(p)
        row = {
            "name": n,
            "backing": p.spec.backing,
            # No tested default for some entry key: the operator's
            # entry carries the probe, so this is not a curated plugin.
            "generic": bool(p.spec.requires_entry),
            # The evidence tier (docs/PLUGINS.md § Maturity), declared on the
            # spec and held by a test; capped for an out-of-tree claim whose
            # evidence is not in its distribution, with the reason.
            "maturity": tier,
            "summary": p.spec.summary,
        }
        if note:
            row["maturity_note"] = note
        rows.append(row)
    app.emit(
        {
            "plugins": rows,
            # Always present, usually empty: a consumer can rely on the key.
            "failed": list(ENTRY_POINT_FAILURES),
        }
    )


@app.command(pinned=True, mutating=False)
def plugin_config(surface: str, plugin: str) -> None:
    """Emit the registry, Caddy and env fragments: `plugin-config <surface> <plugin>`."""
    from ..pluginconfig import render

    app.emit(render(surface, plugin))


@app.command(pinned=False, mutating=False, name="catalog-import")
def catalog_import(source: str, surface: str, registry: str = "") -> None:
    """Emit registry fragments from a server.json: `catalog-import <file|URL|name> <surface>`.

    The surface is positional, not `--surface`: beheaxi renders a required
    parameter as a positional argument, like `plugin-config <surface> <plugin>`.

    SOURCE is a file, an http(s) URL, or a registry name (`namespace/name`,
    optionally `@version`) looked up in `--registry` (default: the official
    MCP Registry). Like `plugin-config`, never emits a credential, and the
    `pinned`/`probe` it cannot know are TODOs that fail `registry-lint`.
    """
    from ..catalog import DEFAULT_REGISTRY, import_server, load_source

    server, origin = load_source(source, registry or DEFAULT_REGISTRY)
    app.emit(import_server(server, surface, origin))


@app.command(pinned=False, mutating=False, name="catalog-export")
def catalog_export(
    plugin: str, name: str = "", server_version: str = "", url: str = "", package: str = ""
) -> None:
    """Emit a curated plugin as server.json with its pins and probe: `catalog-export <plugin>`."""
    from ..catalog import export_plugin

    app.emit(
        export_plugin(plugin, name=name, version=server_version, url=url, package=package)
    )


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


@app.command(pinned=False, mutating=True)
def calendar_consent(
    surface: str,
    subject: str = "",
    identity_map: str = "",
    client_id: str = "",
    client_secret_file: str = "",
    port: int = 8765,
    browser: bool = True,
    revoke: bool = False,
    timeout: int = 300,
) -> None:
    """Grant (or --revoke) one user's calendar for a lookup surface; writes the identity map."""
    from ..plugins.calendar.consent import run

    app.emit(
        run(
            load_registry(_registry_path()),
            surface,
            subject=subject,
            identity_map=identity_map,
            client_id=client_id,
            client_secret_file=client_secret_file,
            port=port,
            no_browser=not browser,
            revoke_grant=revoke,
            timeout=timeout,
        )
    )


def main(argv: list[str] | None = None) -> None:
    import sys

    from beheaxi.context import extract_global_flags

    raw = list(sys.argv[1:]) if argv is None else list(argv)
    ctx, rest = extract_global_flags(raw)
    if rest == ["--version"]:
        # beheaxi has no root --version; answered here rather than by
        # re-registering the root callback, which BeheaxiApp owns.
        if ctx.json:
            app.ctx = ctx
            app.emit({"tool": app.name, "version": app.version})
        else:
            print(f"{app.name} {app.version}")
        raise SystemExit(0)
    raise SystemExit(app.main(argv))


if __name__ == "__main__":
    main()

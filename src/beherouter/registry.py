"""registry.toml — the only beherouter state (foundation §6: no DB)."""

import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .errors import UsageError


@dataclass
class RegistryEntry:
    """One surface. `plugin` names a pre-configured recipe; everything else overrides it.

    `pinned` and `probe` are OVERRIDES of a tested default, not required
    knowledge. Forgetting them yields the plugin's verified behaviour rather
    than an unprobed surface — which is the gitea-home mistake, made unmakeable.
    """

    name: str
    plugin: str
    config: dict | None = None
    env: dict[str, str] | None = None  # logical credential name -> ${VAR}
    pinned: list[str] | None = None
    probe: str | None = None
    probe_args: dict | None = None
    catalogue_ttl_ms: int | None = None  # override the plugin's tested default
    identity: dict | None = None  # per-request identity; see identity.py
    # Per-surface role gating. Separate from `identity` on purpose: a
    # surface may gate without forwarding anything, and gating needs no
    # IdentitySupport from the plugin.
    authz: dict | None = None


def validate_entry(e: RegistryEntry) -> None:
    """Raise UsageError if the entry can't describe a loadable surface.

    Performs NO I/O: this is what `registry-lint` runs on a workstation, because
    an attach failure crash-loops the gateway and takes /healthz with it.
    """
    # Deferred so that importing the registry SCHEMA does not drag in every
    # plugin — and, through them, fastmcp. `load_registry`/`save_registry` stay
    # cheap for callers that only read or write the file.
    from .plugins import get
    from .plugins.validate import validate_config

    plugin = get(e.plugin)  # raises UsageError naming the known plugins
    config = validate_config(e.name, plugin.spec, e.config)
    if plugin.validate is not None:
        plugin.validate(config)
    from .identity import validate_authz, validate_identity

    validate_identity(e.name, plugin.spec, e.identity)
    validate_authz(e.name, e.authz)
    ttl = e.catalogue_ttl_ms
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0):
        raise UsageError(
            f"'{e.name}': catalogue_ttl_ms must be a non-negative integer, got {ttl!r}"
        )
    # `pinned` and `probe_args` are hand-edited far more often than they are
    # generated, and neither fails loudly downstream. A `pinned` string is
    # iterated character by character by the health pin-check, which then
    # reports letters as missing tools; a malformed `probe_args` survives all
    # the way to the probe call. Both are cheap to refuse here, where
    # `registry-lint` runs on a workstation.
    pinned = e.pinned
    if pinned is not None and (
        isinstance(pinned, str)
        or not isinstance(pinned, (list, tuple))
        or not all(isinstance(n, str) for n in pinned)
    ):
        raise UsageError(
            f"'{e.name}': pinned must be an array of tool names, got {pinned!r}"
        )
    if e.probe_args is not None and not isinstance(e.probe_args, dict):
        raise UsageError(
            f"'{e.name}': probe_args must be a table of arguments, got {e.probe_args!r}"
        )
    declared = {v.name for v in plugin.spec.env}
    supplied = set(e.env or {})
    unknown = sorted(supplied - declared)
    if unknown:
        raise UsageError(
            f"'{e.name}': plugin '{e.plugin}' declares no credential(s) {unknown}; "
            f"declared: {sorted(declared) or '(none)'}"
        )
    missing = sorted(declared - supplied)
    if missing:
        raise UsageError(
            f"'{e.name}': plugin '{e.plugin}' requires credential(s) {missing} in [{e.name}.env]"
        )


def load_registry(path: Path) -> dict[str, RegistryEntry]:
    path = Path(path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    known = {f.name for f in fields(RegistryEntry)} - {"name"}
    out: dict[str, RegistryEntry] = {}
    for name, body in data.items():
        unknown = set(body) - known
        if unknown:
            raise UsageError(
                f"'{name}': unknown registry key(s) {sorted(unknown)}; "
                f"allowed: {sorted(known)}"
            )
        out[name] = RegistryEntry(name=name, **body)
    return out


def _toml_value(value) -> str:
    """Render a TOML scalar. bool BEFORE int — bool is a subclass of int.

    Hand-rolled to avoid a tomli-w dependency. A plugin's config table carries
    typed scalars, so serializing everything as a string would silently write
    50 as "50" and fail the plugin's own type check on the next load.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_registry(path: Path, entries: dict[str, RegistryEntry]) -> None:
    lines: list[str] = []
    for name, e in entries.items():
        lines.append(f"[{name}]")
        body = {k: v for k, v in asdict(e).items() if k != "name" and v is not None}
        # Scalars and lists first: TOML binds every bare key that follows a
        # `[name.env]` header to THAT sub-table, so emitting a sub-table early
        # would silently reparent the keys after it.
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        for k, v in body.items():
            if isinstance(v, dict):
                continue
            if isinstance(v, list):
                inner = ", ".join(_toml_value(x) for x in v)
                lines.append(f"{k} = [{inner}]")
            else:
                lines.append(f"{k} = {_toml_value(v)}")
        for k, table in tables.items():
            lines.append("")
            lines.append(f"[{name}.{k}]")
            for tk, tv in table.items():
                lines.append(f"{tk} = {_toml_value(tv)}")
        lines.append("")
    Path(path).write_text("\n".join(lines))

"""registry.toml — the only beherouter state (foundation §6: no DB)."""

import json
import math
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import tomlkit
from tomlkit.items import Table

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
    # Seconds one backend call may take; falls back to $BEHEROUTER_CALL_TIMEOUT_S,
    # and to no limit at all — an upgrade must not start cutting slow backends off.
    call_timeout_s: float | None = None
    identity: dict | None = None  # per-request identity; see identity.py
    # Per-surface role gating. Separate from `identity` on purpose: a
    # surface may gate without forwarding anything, and gating needs no
    # IdentitySupport from the plugin.
    authz: dict | None = None
    # Per-caller token bucket on calls that reach the backend; see gates.py.
    rate_limit: dict | None = None
    # Extra search words, ADDED to the plugin's own; see plugins.resolve_aliases.
    search_aliases: dict[str, list[str]] | None = None


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
    for key in plugin.spec.requires_entry:
        if not getattr(e, key, None):
            raise UsageError(
                f"'{e.name}': plugin '{e.plugin}' has no tested default for "
                f"'{key}'; set `{key}` in [{e.name}]"
            )
    # A generic source publishes only what its config selects; a pin or probe
    # outside that set would attach green and then fail at health.
    published = plugin.published(config) if plugin.published is not None else None
    if published is not None:
        outside = sorted(set(e.pinned or []) - published)
        if outside:
            raise UsageError(
                f"'{e.name}': pinned names {outside}, which plugin '{e.plugin}' "
                f"does not publish from this config"
            )
        if e.probe and e.probe not in published:
            raise UsageError(
                f"'{e.name}': probe '{e.probe}' is a tool plugin '{e.plugin}' "
                f"does not publish from this config"
            )
    from .identity import validate_authz, validate_identity

    validate_identity(e.name, plugin.spec, e.identity)
    validate_authz(e.name, e.authz)
    from .gates import validate_rate_limit

    validate_rate_limit(e.name, e.rate_limit)
    ttl = e.catalogue_ttl_ms
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0):
        raise UsageError(
            f"'{e.name}': catalogue_ttl_ms must be a non-negative integer, got {ttl!r}"
        )
    limit = e.call_timeout_s
    if limit is not None and (
        not isinstance(limit, (int, float))
        or isinstance(limit, bool)
        or not math.isfinite(limit)
        or limit <= 0
    ):
        raise UsageError(
            f"'{e.name}': call_timeout_s must be a positive number of seconds, got {limit!r}"
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
    aliases = e.search_aliases
    if aliases is not None and (
        not isinstance(aliases, dict)
        or not all(
            isinstance(tool, str)
            and isinstance(words, (list, tuple))
            and all(isinstance(w, str) for w in words)
            for tool, words in aliases.items()
        )
    ):
        raise UsageError(
            f"'{e.name}': search_aliases must be a table of tool = [words], got {aliases!r}"
        )
    declared = {v.name for v in plugin.spec.env}
    supplied = set(e.env or {})
    unknown = sorted(supplied - declared)
    if unknown:
        raise UsageError(
            f"'{e.name}': plugin '{e.plugin}' declares no credential(s) {unknown}; "
            f"declared: {sorted(declared) or '(none)'}"
        )
    required = {v.name for v in plugin.spec.env if v.required}
    missing = sorted(required - supplied)
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


def _check_value(value, where: str) -> None:
    """Refuse what registry.toml cannot hold as the registry means it.

    A plugin's config carries typed scalars, and nested tables are tables; a
    table INSIDE an array (or any other object) is refused rather than written
    in some form that parses: `attach`/`detach` rewrite entries an operator did
    not touch, and a silently re-shaped entry is worse than a refusal. bool is
    a subclass of int, so it needs no case of its own.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            _check_value(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for x in value:
            if isinstance(x, dict):
                _refuse(x, where)
            _check_value(x, where)
    elif not isinstance(value, (bool, int, float, str)):
        _refuse(value, where)


def _refuse(value, where: str):
    raise UsageError(
        f"'{where}': cannot write a {type(value).__name__} to registry.toml; "
        f"edit this entry by hand"
    )


def _canon(value) -> str:
    """A type-faithful comparison key: `True == 1` in Python, but not on disk."""
    return json.dumps(value, sort_keys=True)


def _item(value):
    """A tomlkit value for plain data; dicts become real `[a.b]` tables."""
    if isinstance(value, dict):
        table = tomlkit.table()
        for k, v in value.items():
            table[k] = _item(v)
        return table
    if isinstance(value, tuple):
        value = list(value)
    return tomlkit.item(value)


def _trailing_trivia(table: Table) -> list:
    """The comments and blank lines tomlkit parked at the END of `table`.

    tomlkit attaches a comment written above `[next]` to the body of the table
    BEFORE it — the deepest, last sub-table of it. Deleting that table would
    delete the next entry's comment with it.
    """
    body = table.value.body
    keyed = [v for k, v in body if k is not None]
    if keyed and isinstance(keyed[-1], Table):
        return _trailing_trivia(keyed[-1])
    trail: list = []
    for k, v in reversed(body):
        if k is not None:
            break
        trail.insert(0, v)
    return trail


def _delete(container, key: str) -> None:
    """Delete `key`, keeping the comments that belong to what follows it.

    A table's trailing trivia is re-homed where the table stood: everything
    after it is lifted out and put back behind those comments. A comment that
    was really ABOUT the deleted table survives too — an orphaned comment is a
    one-line cleanup, a lost one is knowledge gone.
    """
    item = container[key]
    if not isinstance(item, Table):
        del container[key]
        return
    trivia = _trailing_trivia(item)
    keys = list(container)
    following = [(k, container[k]) for k in keys[keys.index(key) + 1 :]]
    for k in keys[keys.index(key) :]:
        container.remove(k)
    for t in trivia:
        container.append(None, t)
    for k, v in following:
        # `append` gives a table a leading blank line when the item before it
        # is not whitespace; the original spacing is the operator's.
        indent = v.trivia.indent if isinstance(v, Table) else None
        container.append(k, v)
        if indent is not None:
            v.trivia.indent = indent


def _merge(container, data: dict) -> None:
    """Make `container` hold exactly `data`, touching only what differs.

    Untouched keys keep their comments and formatting; that is the whole point.
    A changed scalar is replaced in place, a changed table is merged into
    recursively, so a comment beside an unchanged sibling survives too.
    """
    for key in [k for k in container if k not in data]:
        _delete(container, key)
    for key, value in data.items():
        if key in container:
            current = container[key]
            if isinstance(value, dict) and isinstance(current, dict):
                _merge(current, value)
                continue
            plain = current.unwrap() if hasattr(current, "unwrap") else current
            if _canon(plain) == _canon(value):
                continue
        container[key] = _item(value)


def save_registry(path: Path, entries: dict[str, RegistryEntry]) -> None:
    """Write the registry, or raise before touching the file.

    Round-trips through tomlkit: an existing file is updated in place, so the
    operator's comments, ordering and layout survive `attach`/`detach` — only
    the entries that changed are rewritten, and an unchanged entry is left
    byte for byte.

    ⚠️ Written in place, not temp-and-rename: the registry is commonly a
    single bind-mounted file (a container volume, a ConfigMap subPath), and
    renaming over a mount point fails with EBUSY.
    """
    path = Path(path)
    bodies: dict[str, dict] = {}
    for name, e in entries.items():
        body = {k: v for k, v in asdict(e).items() if k != "name" and v is not None}
        _check_value(body, name)
        bodies[name] = body
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    _merge(doc, bodies)
    path.write_text(tomlkit.dumps(doc))

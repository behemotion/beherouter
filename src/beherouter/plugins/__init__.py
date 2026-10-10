"""The plugin registry.

Two sources, one table. In-tree plugins import themselves at the bottom of this
module; out-of-tree ones are discovered through the `beherouter.plugins` entry
point group, which was the LOOKUP-only extension this protocol was designed for
— `PluginSpec` and `build()` are identical either way, so a third-party backend
no longer needs a fork of the gateway to attach.

⚠️ A third-party package that fails to import is RECORDED AND SKIPPED, not fatal
(logged, and listed in `ENTRY_POINT_FAILURES` for `plugins` and `registry-lint`):
one broken dependency must not cost the gateway every other plugin, and an entry
naming the missing plugin still fails loudly and early ("unknown plugin", from
`registry-lint`, before a deploy). Nothing is ever served by a plugin that did
not load.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ..errors import UsageError
from .spec import (
    API_VERSION,
    BACKINGS,
    MATURITY_TIERS,
    SUPPORTED_API_VERSIONS,
    ConfigField,
    EnvVar,
    PluginContext,
    PluginSpec,
)

__all__ = [
    "API_VERSION",
    "BACKINGS",
    "ENTRY_POINT_FAILURES",
    "ENTRY_POINT_GROUP",
    "PLUGINS",
    "ConfigField",
    "EnvVar",
    "Plugin",
    "PluginContext",
    "PluginSpec",
    "get",
    "load_entry_point_plugins",
    "register",
    "resolve_pinned",
    "resolve_probe",
]

logger = logging.getLogger(__name__)

# The group an out-of-tree distribution advertises, e.g. in its pyproject.toml:
#
#     [project.entry-points."beherouter.plugins"]
#     acme-crm = "acme_beherouter.crm"
#
# The value names a MODULE that calls `register()` when imported — the same
# contract every in-tree plugin already follows, so a plugin can move in or out
# of the tree without changing a line.
ENTRY_POINT_GROUP = "beherouter.plugins"


class BuildFn(Protocol):
    def __call__(self, ctx: PluginContext) -> Awaitable: ...


@dataclass(frozen=True)
class Plugin:
    spec: PluginSpec
    build: BuildFn
    validate: Callable[[dict], None] | None = None
    # Legal-but-a-trap findings for `registry-lint`, from the effective config.
    # Never raises for a valid config; warnings never fail the lint.
    warn: Callable[[dict], list[str]] | None = None
    # The tool names this plugin's EFFECTIVE CONFIG publishes, computed offline;
    # None when that cannot be known here (a URL spec, a directory absent on
    # the linting machine). `validate_entry` holds `pinned` and `probe` to it.
    published: Callable[[dict], set[str] | None] | None = None


PLUGINS: dict[str, Plugin] = {}

# Entry points that did not yield a plugin, as {entry_point, value, error}.
# Recorded, not only logged: a log line is invisible to `beherouter plugins`
# and `registry-lint`, the two places an operator looks before a deploy. The
# error is the exception's one-line `Type: message`; the traceback stays in
# the log.
ENTRY_POINT_FAILURES: list[dict[str, str]] = []


def register(spec: PluginSpec, build: BuildFn, validate=None, warn=None, published=None) -> None:
    """Add a plugin. Duplicate names are a programming error, not a config one."""
    if spec.api not in SUPPORTED_API_VERSIONS:
        served = ", ".join(f"v{v}" for v in SUPPORTED_API_VERSIONS)
        raise UsageError(
            f"plugin '{spec.name}' is written for plugin API v{spec.api}; this "
            f"gateway serves {served}. Install a release of the plugin built "
            f"for it, or a beherouter that serves v{spec.api}."
        )
    if spec.backing not in BACKINGS:
        raise UsageError(
            f"plugin '{spec.name}': backing must be one of {BACKINGS}, got '{spec.backing}'"
        )
    if spec.maturity not in MATURITY_TIERS:
        raise UsageError(
            f"plugin '{spec.name}': maturity must be one of {MATURITY_TIERS}, "
            f"got '{spec.maturity}'"
        )
    if spec.name in PLUGINS:
        raise UsageError(f"plugin '{spec.name}' is already registered")
    PLUGINS[spec.name] = Plugin(
        spec=spec, build=build, validate=validate, warn=warn, published=published
    )


def load_entry_point_plugins(eps=None) -> list[str]:
    """Import every distribution advertising `beherouter.plugins`.

    Returns the names that actually ADDED a plugin — an entry point whose module
    registers nothing, or whose name collides with one already registered, is
    not reported as loaded, because reporting it would tell an operator a plugin
    is available that `get()` cannot find.

    `eps` is injectable so the contract is testable without installing a package.
    """
    if eps is None:
        from importlib.metadata import entry_points

        eps = entry_points(group=ENTRY_POINT_GROUP)
    loaded: list[str] = []
    for ep in eps:
        before = set(PLUGINS)
        try:
            ep.load()
        except Exception as e:
            ENTRY_POINT_FAILURES.append(
                {
                    "entry_point": str(getattr(ep, "name", "?")),
                    "value": str(getattr(ep, "value", "?")),
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            # Bare name and value only: a third-party traceback belongs in the
            # log, not in the message an operator reads first.
            logger.warning(
                "plugin entry point %r (%s) failed to load; it will not be "
                "available to the registry",
                getattr(ep, "name", "?"),
                getattr(ep, "value", "?"),
                exc_info=True,
            )
            continue
        added = sorted(set(PLUGINS) - before)
        if not added:
            ENTRY_POINT_FAILURES.append(
                {
                    "entry_point": str(ep.name),
                    "value": str(ep.value),
                    "error": "registered nothing (no register() call, or a name "
                    "already taken)",
                }
            )
            logger.warning(
                "plugin entry point %r (%s) registered nothing", ep.name, ep.value
            )
            continue
        loaded.extend(added)
    return loaded


def get(name: str) -> Plugin:
    """Look up a plugin, naming the alternatives when it is missing."""
    try:
        return PLUGINS[name]
    except KeyError:
        known = ", ".join(sorted(PLUGINS)) or "(none registered)"
        # The likeliest reason an out-of-tree plugin is unknown is that its
        # package failed to import at startup; name those rather than leave
        # the operator to find the warning in a log.
        hint = (
            "; plugin entry point(s) that failed to load: "
            + ", ".join(f"{f['entry_point']} ({f['value']})" for f in ENTRY_POINT_FAILURES)
            + " (see `beherouter plugins`)"
            if ENTRY_POINT_FAILURES
            else ""
        )
        raise UsageError(
            f"unknown plugin '{name}'; known plugins: {known}{hint}"
        ) from None


def resolve_pinned(entry, plugin: "Plugin | None") -> list[str]:
    """The pin list for a registry entry: its override, else the plugin's default.

    ONE resolver, because `probe`/`probe_args` already taught this codebase what
    two call sites deciding the same thing separately costs — one gets enriched,
    they diverge, and the surface calls the right tool with the wrong arguments.
    `gateway.load_backend` and `health.check_entry` differ in how they LOOK UP
    the plugin (the first raises on an unknown one; the second tolerates it, so a
    bad registry still reports rather than explodes) — hence the plugin is passed
    in rather than fetched here. They must not also differ in what they do with
    it.
    """
    if entry.pinned is not None:
        return list(entry.pinned)
    return list(plugin.spec.pinned) if plugin is not None else []


def resolve_probe(entry, plugin: "Plugin | None") -> tuple[str | None, dict | None]:
    """The (probe, probe_args) pair for a registry entry, resolved as ONE unit.

    ⚠️ Never field by field: an entry's overriding probe with the PLUGIN's
    arguments calls the right tool with another tool's arguments — `get_me`
    invoked with {"query": "pdf"} fails on an unexpected keyword and reads like
    a dead credential. Same plugin-passed-in shape as `resolve_pinned`, for the
    same reason: `health` tolerates an unknown plugin (→ no probe).
    """
    if entry.probe:
        return entry.probe, entry.probe_args
    if plugin is not None:
        return plugin.spec.probe, plugin.spec.probe_args
    return None, None


def resolve_aliases(entry, plugin: "Plugin | None") -> dict[str, tuple[str, ...]]:
    """A surface's search vocabulary: the plugin's, UNION the entry's additions.

    Additive, unlike `pinned`: a tested vocabulary cannot be lost by an operator
    adding one word. Order-preserving and de-duplicated.
    """
    merged = {
        k: tuple(v) for k, v in (plugin.spec.search_aliases if plugin else {}).items()
    }
    for tool, words in (entry.search_aliases or {}).items():
        merged[tool] = tuple(dict.fromkeys([*merged.get(tool, ()), *words]))
    return merged


# Below the registry machinery on purpose: each module calls `register` at import.
from . import (  # noqa: E402, F401  (imported for their registration side effect)
    beheaxi_cli,
    gcal,
    m365,
    mcp_http,
    mcp_stdio,
    office_mcp,
    openapi,
    plane,
    plane_http,
    plane_http_apikey,
    python_dir,
    sonarqube,
)

# Out-of-tree plugins, AFTER the in-tree ones: `register` refuses a duplicate
# name, so an external package cannot shadow a plugin that ships here.
load_entry_point_plugins()

# A test fixture on the `cli` backing with no mandatory probe, which keeps the
# cli load path cheap to exercise. Kept out of `beherouter plugins` so no agent
# pays context for it; production uses `beheaxi-cli`.
if os.environ.get("BEHEROUTER_TEST_PLUGINS") == "1":  # pragma: no branch - suite sets it
    from . import _test_cli  # noqa: F401

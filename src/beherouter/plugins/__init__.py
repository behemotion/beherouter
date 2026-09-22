"""The plugin registry.

In-tree today: plugins import themselves at the bottom of this module. The
protocol is designed so that out-of-tree discovery is a change of LOOKUP only —

    for ep in entry_points(group="beherouter.plugins"):
        ep.load()

— with no change to PluginSpec or build().
"""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ..errors import UsageError
from .spec import BACKINGS, ConfigField, EnvVar, PluginContext, PluginSpec

__all__ = [
    "BACKINGS",
    "PLUGINS",
    "ConfigField",
    "EnvVar",
    "Plugin",
    "PluginContext",
    "PluginSpec",
    "get",
    "register",
    "resolve_pinned",
]


class BuildFn(Protocol):
    def __call__(self, ctx: PluginContext) -> Awaitable: ...


@dataclass(frozen=True)
class Plugin:
    spec: PluginSpec
    build: BuildFn
    validate: Callable[[dict], None] | None = None


PLUGINS: dict[str, Plugin] = {}


def register(spec: PluginSpec, build: BuildFn, validate=None) -> None:
    """Add a plugin. Duplicate names are a programming error, not a config one."""
    if spec.backing not in BACKINGS:
        raise UsageError(
            f"plugin '{spec.name}': backing must be one of {BACKINGS}, got '{spec.backing}'"
        )
    if spec.name in PLUGINS:
        raise UsageError(f"plugin '{spec.name}' is already registered")
    PLUGINS[spec.name] = Plugin(spec=spec, build=build, validate=validate)


def get(name: str) -> Plugin:
    """Look up a plugin, naming the alternatives when it is missing."""
    try:
        return PLUGINS[name]
    except KeyError:
        known = ", ".join(sorted(PLUGINS)) or "(none registered)"
        raise UsageError(f"unknown plugin '{name}'; known plugins: {known}") from None


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


from . import (  # noqa: F401  (imported for their registration side effect)
    gcal,
    m365,
    office_mcp,
    plane,
    plane_http,
    plane_http_apikey,
)

# The `cli` backing ships with no production plugin. Its one caller is a test
# fixture, kept out of `beherouter plugins` so no agent pays context for it.
if os.environ.get("BEHEROUTER_TEST_PLUGINS") == "1":
    from . import _test_cli  # noqa: F401

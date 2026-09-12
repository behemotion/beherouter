"""The declarative half of a plugin: frozen data, no behaviour, no I/O.

Three consumers must read a plugin without attaching it — `plugin-config`,
`registry-lint` and `plugin list`. Keeping the declaration inert is also what
makes "attach performs no network I/O" mechanically enforceable rather than a
convention: a plugin CANNOT phone home from its spec, only from build().
"""

from dataclasses import dataclass, field

BACKINGS = ("native", "http", "stdio", "cli")


@dataclass(frozen=True)
class ConfigField:
    """One key in a plugin's `[surface.config]` table."""

    name: str
    type: type  # str | int | bool | float
    required: bool = False
    default: object = None
    doc: str = ""


@dataclass(frozen=True)
class EnvVar:
    """A credential the plugin needs, by LOGICAL name.

    The plugin maps this to wherever the credential actually goes — a subprocess
    environment variable, an HTTP header, an OAuth exchange. The operator never
    needs to know the backend's own variable name.
    """

    name: str
    doc: str = ""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    summary: str
    backing: str
    pinned: tuple[str, ...] = ()
    probe: str | None = None
    probe_args: dict | None = None
    config: tuple[ConfigField, ...] = ()
    env: tuple[EnvVar, ...] = ()
    # How long this backend's catalogue stays fresh, in milliseconds. A tested
    # default the operator overrides per entry, exactly like `pinned` and
    # `probe`. 0 disables refresh.
    catalogue_ttl_ms: int = 300_000


@dataclass(frozen=True)
class PluginContext:
    """Everything a build() needs, already validated and resolved."""

    surface: str
    config: dict
    env: dict[str, str] = field(default_factory=dict)
    pinned: list[str] = field(default_factory=list)
    probe: str | None = None
    probe_args: dict | None = None

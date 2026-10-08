"""The declarative half of a plugin: frozen data, no behaviour, no I/O.

Three consumers must read a plugin without attaching it — `plugin-config`,
`registry-lint` and `plugin list`. Keeping the declaration inert is also what
makes "attach performs no network I/O" mechanically enforceable rather than a
convention: a plugin CANNOT phone home from its spec, only from build().
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

BACKINGS = ("native", "http", "stdio", "cli", "inproc")

# Maturity tiers, lowest first. A tier is a claim about EVIDENCE: every tier
# above `declared` is checked by `beherouter.testing.plugin_conformance`, never
# merely asserted, and each tier includes the checks of the ones below it.
# Declared on the spec, never computed at runtime: the gateway does not run
# pytest, and what it CAN check live is already `health --deep`'s job.
MATURITY_TIERS = ("declared", "probed", "catalogued", "verified", "per-user")

# The plugin contract's version. Bump ONLY on a change an existing plugin
# cannot survive (a removed or re-typed PluginSpec/PluginContext field, a
# changed build() contract); additive fields keep the number. A gateway
# serving several versions lists them all.
API_VERSION = 1
SUPPORTED_API_VERSIONS: tuple[int, ...] = (API_VERSION,)


@dataclass(frozen=True)
class ConfigField:
    """One key in a plugin's `[surface.config]` table."""

    name: str
    type: type  # str | int | bool | float | list
    required: bool = False
    default: object = None
    doc: str = ""


@dataclass(frozen=True)
class EnvVar:
    """A credential the plugin needs, by LOGICAL name.

    The plugin maps this to wherever the credential actually goes — a subprocess
    environment variable, an HTTP header, an OAuth exchange. The operator never
    needs to know the backend's own variable name.

    `required=False` is for a generic plugin whose backend may or may not take
    a credential (`mcp-http`, `mcp-stdio`): there is no tested answer to "does
    this backend need one", so the operator's entry is the answer. A curated
    plugin knows, and keeps the default.
    """

    name: str
    doc: str = ""
    required: bool = True


@dataclass(frozen=True)
class IdentitySupport:
    """Whether a plugin can carry a PER-REQUEST identity, and where it lands.

    The default is NO MODES, which is the fail-closed half: a plugin nobody has
    audited for per-user use cannot be configured for it, and `registry-lint`
    says so offline rather than a deploy saying it at 3am.

    `target` follows the backing — `header` for http, `env` for cli,
    `credential` for native, and stdio cannot (a subprocess environment is
    fixed at spawn and `keep_alive=True` reuses it across callers).

    `accepts` closes the target namespace. Empty means "any name" for `header`
    and `env`, where the backend's vocabulary is open. For `credential` it is
    REQUIRED and must be a subset of this plugin's own `env` names.
    """

    modes: tuple[str, ...] = ()
    target: str = ""
    accepts: tuple[str, ...] = ()
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
    # Whether this plugin may carry a per-request identity; see identity.py.
    identity: IdentitySupport = IdentitySupport()
    # Extra search words per tool name: words an agent would TYPE that the
    # backend's own description does not contain (Plane says `cycle`, agents
    # say "sprint"). A tested default; a registry entry may ADD to it, never
    # replace it. See docs/PLUGINS.md § Search vocabulary.
    search_aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Registry-entry keys that are MANDATORY for this plugin: the one place
    # `probe`/`pinned` stop being overrides, because there is no tested
    # default to fall back to (a generic source like `openapi`).
    requires_entry: tuple[str, ...] = ()
    # The tier this plugin claims (one of MATURITY_TIERS) and the in-tree
    # paths that prove it: recorded catalogues (`*.json`, a `tools/list`
    # reply) and test ids (`path::test_name`, or `path::<check name>` for the
    # e2e driver's named checks). Paths are relative to the distribution root.
    # ⚠️ Additive fields, so API_VERSION does not move; the default claims
    # nothing, which is the honest default for a spec nobody has tested.
    maturity: str = "declared"
    evidence: tuple[str, ...] = ()
    # Which plugin contract this spec was written against; `register()`
    # refuses one this gateway does not serve. See API_VERSION.
    api: int = API_VERSION


@dataclass(frozen=True)
class PluginContext:
    """Everything a build() needs, already validated and resolved."""

    surface: str
    config: dict
    env: dict[str, str] = field(default_factory=dict)
    pinned: list[str] = field(default_factory=list)
    probe: str | None = None
    probe_args: dict | None = None

"""The supported import surface for writing a beherouter plugin.

    from beherouter.plugin_api import PluginSpec, McpBacking, load_mcp_backend, load_inproc_backend, register

Everything a plugin needs is here, and ONLY what is here carries a
compatibility promise: the names below keep working across patch and minor
releases, and a change a plugin cannot survive bumps `API_VERSION`, which
`register()` checks. Internal paths (`beherouter.backends.mcp`, ...) stay
importable and promise nothing.

⚠️ Names resolve LAZILY, on first access, and that is load-bearing:
`beherouter.plugins` loads entry-point plugins as a side effect of import. If
this module imported it eagerly, a process whose first import is this module
would load external plugins while this module was half-initialised, and every
`from beherouter.plugin_api import register` in them would fail. Held by
tests/test_plugin_api.py::test_an_external_plugin_registers_when_the_facade_is_the_first_import.
"""

import importlib
from typing import TYPE_CHECKING

_EXPORTS: dict[str, str] = {
    "API_VERSION": "beherouter.plugins.spec",
    "ConfigField": "beherouter.plugins.spec",
    "EnvVar": "beherouter.plugins.spec",
    "IdentitySupport": "beherouter.plugins.spec",
    "PluginContext": "beherouter.plugins.spec",
    "PluginSpec": "beherouter.plugins.spec",
    "register": "beherouter.plugins",
    "McpBacking": "beherouter.backends.backing",
    "CliBacking": "beherouter.backends.backing",
    "load_mcp_backend": "beherouter.backends.mcp",
    "load_inproc_backend": "beherouter.backends.inproc",
    "identity_client": "beherouter.backends.inproc",
    "load_cli_backend": "beherouter.backends.cli",
    "Backend": "beherouter.models",
    "ToolDescriptor": "beherouter.models",
    "AuthError": "beherouter.errors",
    "Unavailable": "beherouter.errors",
    "UsageError": "beherouter.errors",
}

# A literal, not sorted(_EXPORTS), so linters and type checkers see the
# re-exports; tests/test_plugin_api.py holds it to the documented set and
# resolves every name in it through _EXPORTS.
__all__ = [
    "API_VERSION",
    "AuthError",
    "Backend",
    "CliBacking",
    "ConfigField",
    "EnvVar",
    "IdentitySupport",
    "McpBacking",
    "PluginContext",
    "PluginSpec",
    "ToolDescriptor",
    "Unavailable",
    "UsageError",
    "identity_client",
    "load_cli_backend",
    "load_inproc_backend",
    "load_mcp_backend",
    "register",
]


def __getattr__(name: str):
    try:
        module = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'beherouter.plugin_api' has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # resolve once
    return value


def __dir__() -> list[str]:
    return __all__


if TYPE_CHECKING:  # for type checkers and IDEs only; never executed
    from .backends.backing import CliBacking, McpBacking
    from .backends.cli import load_cli_backend
    from .backends.inproc import identity_client, load_inproc_backend
    from .backends.mcp import load_mcp_backend
    from .errors import AuthError, Unavailable, UsageError
    from .models import Backend, ToolDescriptor
    from .plugins import register
    from .plugins.spec import (
        API_VERSION,
        ConfigField,
        EnvVar,
        IdentitySupport,
        PluginContext,
        PluginSpec,
    )

"""Validate a registry entry's `config` table against its plugin's declaration.

Deliberately free of I/O and of plugin instantiation: `registry-lint` runs this
on a workstation to catch a bad registry BEFORE a deploy, because an attach
failure crash-loops the gateway and takes /healthz with it.
"""

from ..errors import UsageError
from .spec import PluginSpec


def validate_config(surface: str, spec: PluginSpec, raw: dict | None) -> dict:
    """Return the effective config: supplied values over declared defaults."""
    supplied = dict(raw or {})
    fields = {f.name: f for f in spec.config}

    if not fields:
        if supplied:
            raise UsageError(
                f"'{surface}': plugin '{spec.name}' takes no config, got {sorted(supplied)}"
            )
        return {}

    unknown = sorted(set(supplied) - set(fields))
    if unknown:
        raise UsageError(
            f"'{surface}': unknown config key(s) {unknown} for plugin "
            f"'{spec.name}'; allowed: {sorted(fields)}"
        )

    out: dict = {}
    for name, f in fields.items():
        if name not in supplied:
            if f.required:
                raise UsageError(
                    f"'{surface}': plugin '{spec.name}' requires config '{name}'"
                    + (f" ({f.doc})" if f.doc else "")
                )
            out[name] = f.default
            continue
        value = supplied[name]
        # bool is a subclass of int in Python; accepting True for an int field
        # would silently turn a typo into the value 1.
        wrong = not isinstance(value, f.type) or (f.type is int and isinstance(value, bool))
        if wrong:
            raise UsageError(
                f"'{surface}': config '{name}' must be {f.type.__name__}, "
                f"got {type(value).__name__}"
            )
        out[name] = value
    return out

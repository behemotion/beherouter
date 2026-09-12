"""Resolve whole-value `${VAR}` placeholders against the gateway's environment.

Shared by every backing so the rule cannot drift. Only a value that is EXACTLY
one placeholder is substituted — no partial or recursive interpolation. That
keeps the rule easy to audit and means a literal value containing a brace can
never be mistaken for a secret reference.

An unset OR EMPTY variable is a UsageError rather than an empty string: an empty
credential yields a surface that attaches cleanly and then fails every call,
which is precisely the silent breakage this path exists to prevent (observed
2026-07-30 with a revoked Gitea PAT).
"""

import os
import re

from .errors import UsageError

PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def expand(context: str, values: dict[str, str]) -> dict[str, str]:
    """Resolve placeholders in `values`. `context` names the caller for errors."""
    out: dict[str, str] = {}
    for key, value in values.items():
        m = PLACEHOLDER.match(value) if isinstance(value, str) else None
        if m is None:
            out[key] = value
            continue
        var = m.group(1)
        resolved = os.environ.get(var)
        if resolved is None or resolved == "":
            raise UsageError(
                f"'{context}': '{key}' references ${{{var}}}, which is "
                f"unset or empty in the gateway's environment"
            )
        out[key] = resolved
    return out

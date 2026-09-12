"""Emit the three deployment fragments a surface needs, from one inert spec.

Per-surface plumbing is three edits across two repos — a registry block here, a
`not` clause in the reverse-proxy vhost, and a token line in the gateway's env —
and the failure modes are asymmetric: registry-only leaves the surface
unreachable (default-deny, safe), while registry+env WITHOUT the Caddy clause
leaves it answering another client's token (not safe). Generating all three from
one declaration is what stops them disagreeing.

Like `client-config`, this NEVER emits a credential — only the placeholder and
the vault variable name.
"""

import re

from .errors import UsageError
from .plugins import get

_SURFACE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def token_var(surface: str, credential: str) -> str:
    return f"BEHEROUTER_{surface.upper().replace('-', '_')}_{credential.upper()}"


def render(surface: str, plugin_name: str) -> dict:
    """Return {'registry': str, 'caddy': str, 'env': str}."""
    if not _SURFACE.match(surface):
        raise UsageError(
            f"surface '{surface}' must be lowercase alphanumeric with dashes: "
            f"it becomes a URL path and a Caddy matcher name"
        )
    spec = get(plugin_name).spec

    lines = [f"[{surface}]", f'plugin = "{plugin_name}"']
    required = [f for f in spec.config if f.required]
    if required:
        lines.append(f"  [{surface}.config]")
        for f in required:
            lines.append(f'  {f.name} = ""    # {f.doc or "required"}')
    if spec.env:
        lines.append(f"  [{surface}.env]")
        for v in spec.env:
            lines.append(f'  {v.name} = "${{{token_var(surface, v.name)}}}"')

    caddy = (
        f"# --- Caddyfile, inside the beherouter vhost matcher ---\n"
        f"  not path_regexp {surface} ^/{surface}/mcp/?$"
    )

    env = ""
    if spec.env:
        env_lines = ["# --- the gateway's .env (or its template) ---"]
        for v in spec.env:
            var = token_var(surface, v.name)
            env_lines.append(f"{var}={{{{ vault_{var.lower()} }}}}")
        env = "\n".join(env_lines)

    return {
        "registry": "# --- registry.toml ---\n" + "\n".join(lines),
        "caddy": caddy,
        "env": env,
    }

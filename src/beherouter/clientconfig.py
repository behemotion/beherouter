"""Render the registry into paste-ready config for each agent client.

The marked goal: adding a service to every agent must be one registry block
plus re-running this, never N hand-edits across N client files.

This NEVER emits a credential. Each surface's real bearer token lives in the
ansible vault and is checked by Caddy; what goes here is the placeholder the
operator fills in, so the output is safe to paste anywhere.

**Every client speaks a different dialect**, established empirically — the
first four on 2026-08-04, hermes on 2026-08-08 — by pasting this output
unmodified into each and having an agent call a tool through it. A single shape
cannot satisfy all five:

| client      | key           | transport         | credential placeholder |
|-------------|---------------|-------------------|------------------------|
| claude-code | `mcpServers`  | `http`            | `${VAR}` from env      |
| pi          | `mcpServers`  | `http`            | `${VAR}` from env      |
| opencode    | `mcp`         | `remote`          | `{env:VAR}`            |
| librechat   | `mcpServers`  | `streamable-http` | `{{var}}` user-entered |
| hermes      | `mcp_servers` | inferred from url | `${VAR}` from env      |

The failure modes are asymmetric, which is why the wrong shape is worse than no
shape: OpenCode rejects `mcpServers` loudly ("Unrecognized key"), but LibreChat
and Hermes both accept a `${VAR}` header they cannot resolve and forward it to
Caddy *verbatim*, so the only symptom is a 401 that looks like a bad token.

Note the emitted mapping is JSON, and JSON is valid YAML — which is what makes
one renderer serve Hermes's `config.yaml` as well as the JSON-configured four.
"""

import os

from .errors import UsageError

AGENTS = ("librechat", "claude-code", "pi", "opencode", "hermes")

# The published gateway URL a generated client config should point at. A real
# host must never be the default: `client-config` is run by strangers, and a
# baked-in hostname sends their agent at someone else's gateway. Precedence is
# explicit flag > BEHEROUTER_PUBLIC_URL > this.
DEFAULT_PUBLIC_URL = "http://localhost:47100"


def resolve_public_url(explicit: str | None = None) -> str:
    """Resolve the base URL for a generated client config.

    Kept out of the Typer signature on purpose: a default evaluated at import
    time cannot honour an environment variable set afterwards, and resolving
    here makes the precedence testable without invoking the CLI.
    """
    if explicit:
        return explicit
    return os.environ.get("BEHEROUTER_PUBLIC_URL") or DEFAULT_PUBLIC_URL


def _token_var(surface: str) -> str:
    return f"BEHEROUTER_{surface.upper().replace('-', '_')}_TOKEN"


def _url(base_url: str, surface: str) -> str:
    # The canonical path is the BARE /<surface>/mcp. A trailing slash gets a 307
    # from the backend's Starlette Mount, built from the plain-HTTP loopback
    # request the proxy makes — which would bounce a client to http on the LAN
    # carrying its token. Caddy strips the slash; do not add one back here.
    return f"{base_url.rstrip('/')}/{surface}/mcp"


def _auth(placeholder: str) -> dict:
    return {"Authorization": f"Bearer {placeholder}"}


def _mcp_servers(surfaces: list[str], base_url: str) -> dict:
    """The `mcpServers` + `type: http` + `${VAR}` dialect: claude-code and pi.

    Both expand `${VAR}` from the process environment, so the placeholder the
    operator exports is the whole of the wiring.
    """
    return {
        s: {
            "type": "http",
            "url": _url(base_url, s),
            "headers": _auth(f"${{{_token_var(s)}}}"),
        }
        for s in surfaces
    }


def _opencode(surfaces: list[str], base_url: str) -> dict:
    """OpenCode's own dialect.

    `mcpServers` is not merely ignored — it aborts startup with
    "Configuration is invalid ... Unrecognized key: mcpServers". Remote servers
    are `type: remote`, must be explicitly `enabled`, and substitute the
    environment with `{env:VAR}`; `${VAR}` passes through unexpanded.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            s: {
                "type": "remote",
                "url": _url(base_url, s),
                "enabled": True,
                "headers": _auth(f"{{env:{_token_var(s)}}}"),
            }
            for s in surfaces
        },
    }


def _hermes(surfaces: list[str], base_url: str) -> dict:
    """Hermes Agent's dialect — snake_case, and no transport key at all.

    The key is `mcp_servers` in `~/.hermes/config.yaml`. Hermes picks the
    transport from the entry's shape: `url` means HTTP, `command` means stdio.
    There is no `type` field, so none is emitted; `type: http` is accepted but
    ignored, and emitting it would advertise a key Hermes never reads.

    `${VAR}` is resolved by `tools.mcp_tool._interpolate_env_vars` from the
    active profile's secret scope, falling back to `os.environ` — which is why
    the harness-wide `BEHEROUTER_<SURFACE>_TOKEN` works unchanged. Cursor-style
    `${env:VAR}` resolves identically (the prefix is stripped), so this is the
    one client that would also accept OpenCode's spelling.

    Provision by pasting, NOT with `hermes mcp add`: that command derives its
    own key `MCP_<NAME>_API_KEY` and stores the token in the profile's `.env`,
    forking the harness naming convention for no gain.

    An unset variable keeps the literal placeholder instead of raising, so
    `Bearer ${...}` is sent to Caddy as-is and 401s — LibreChat's trap exactly.

    Verified 2026-08-08 (Hermes v0.19.0, isolated `HERMES_HOME`, hermes VM):
    connected in 245ms, discovered the gateway-only `search_tools`, and a real
    one-shot agent turn called `search_tools("discovr")` and got back the fuzzy
    tier — `discover` and `invoke`.
    """
    return {
        "mcp_servers": {
            s: {
                "url": _url(base_url, s),
                "headers": _auth(f"${{{_token_var(s)}}}"),
            }
            for s in surfaces
        }
    }


def _librechat(surfaces: list[str], base_url: str, host: str) -> dict:
    """LibreChat's dialect, plus the two steps a human forgets.

    `headers` values are never substituted from the environment: LibreChat's
    StreamableHTTPOptionsSchema declares them as a plain string record with no
    extractEnvVariable transform, unlike `url` on that same schema. A `${VAR}`
    header therefore reaches Caddy as the literal string and 401s. The one
    substitution path that works for MCP headers is `{{var}}` templating fed by
    `customUserVars`, which each user pastes once in the server's settings.

    `requiresOAuth: false` is required, not cosmetic. Caddy hard-401s every
    surface on every path and publishes no OAuth discovery metadata.
    LibreChat's auto-detection probes the bare URL with no headers at all —
    customUserVars are not known that early — sees the 401 and misclassifies
    the server as OAuth-protected, producing a "Needs Auth" badge whose
    Authenticate button 404s. Setting it explicitly makes detectOAuth() skip.

    Without `allowedDomains` LibreChat's SSRF guard refuses the host with no
    useful error — and every surface here resolves, by split-horizon DNS, to a
    private address, so it always needs the entry.
    """
    servers = {
        s: {
            "type": "streamable-http",
            "url": _url(base_url, s),
            "requiresOAuth": False,
            "headers": _auth(f"{{{{{_token_var(s)}}}}}"),
            "customUserVars": {
                _token_var(s): {
                    "title": f"beherouter '{s}' surface token",
                    "description": (
                        f"Bearer token for {_url(base_url, s)}. Shared homelab "
                        "token, not personal — the same value for every user; it "
                        "is checked by Caddy at the edge. Paste it once in this "
                        "server's settings. Rotating the vault key does not "
                        "update what a user already pasted."
                    ),
                }
            },
        }
        for s in surfaces
    }
    return {"mcpServers": servers, "mcpSettings": {"allowedDomains": [host]}}


def render(agent: str, surfaces: list[str], base_url: str) -> dict:
    if agent not in AGENTS:
        raise UsageError(f"unknown agent '{agent}'; expected one of {', '.join(AGENTS)}")
    host = base_url.split("://", 1)[-1].split("/", 1)[0]

    if agent == "opencode":
        return _opencode(surfaces, base_url)
    if agent == "librechat":
        return _librechat(surfaces, base_url, host)
    if agent == "hermes":
        return _hermes(surfaces, base_url)
    return {"mcpServers": _mcp_servers(surfaces, base_url)}

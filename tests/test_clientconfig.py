import pytest

from beherouter.clientconfig import AGENTS, render
from beherouter.errors import UsageError

BASE = "https://gateway.example.com"


def test_librechat_emits_one_entry_per_surface():
    out = render("librechat", ["gitea", "office"], BASE)
    assert set(out["mcpServers"]) == {"gitea", "office"}
    assert out["mcpServers"]["gitea"]["url"] == f"{BASE}/gitea/mcp"


def test_librechat_includes_allowed_domains():
    """The step a human forgets — LibreChat silently fails without it."""
    out = render("librechat", ["gitea"], BASE)
    assert "gateway.example.com" in out["mcpSettings"]["allowedDomains"]


def test_claude_code_shape():
    out = render("claude-code", ["gitea"], BASE)
    entry = out["mcpServers"]["gitea"]
    assert entry["type"] == "http"
    assert entry["url"] == f"{BASE}/gitea/mcp"


def test_pi_shape_matches_claude_code():
    """pi reads ~/.pi/agent/mcp.json and expands ${VAR} from the environment.

    Verified 2026-08-04 by running pi against this exact output unmodified.
    """
    out = render("pi", ["office"], BASE)
    entry = out["mcpServers"]["office"]
    assert entry["url"] == f"{BASE}/office/mcp"
    assert entry["headers"]["Authorization"] == "Bearer ${BEHEROUTER_OFFICE_TOKEN}"


def test_opencode_uses_its_own_mcp_key_not_mcpservers():
    """OpenCode hard-errors on `mcpServers`: "Configuration is invalid ...

    Unrecognized key: mcpServers". Its key is `mcp` and its remote type is
    `remote`, not `http`.
    """
    out = render("opencode", ["office"], BASE)
    assert "mcpServers" not in out
    entry = out["mcp"]["office"]
    assert entry["type"] == "remote"
    assert entry["enabled"] is True
    assert entry["url"] == f"{BASE}/office/mcp"


def test_opencode_uses_brace_env_substitution():
    """`${VAR}` is never expanded by OpenCode; its syntax is `{env:VAR}`."""
    out = render("opencode", ["office"], BASE)
    header = out["mcp"]["office"]["headers"]["Authorization"]
    assert header == "Bearer {env:BEHEROUTER_OFFICE_TOKEN}"


def test_librechat_uses_streamable_http_type():
    """LibreChat's schema names the transport `streamable-http`, not `http`."""
    out = render("librechat", ["office"], BASE)
    assert out["mcpServers"]["office"]["type"] == "streamable-http"


def test_librechat_headers_use_customuservar_templating():
    """LibreChat never substitutes env vars inside `headers`.

    StreamableHTTPOptionsSchema.headers is a plain z.record with no
    extractEnvVariable transform, unlike `url` on the same schema — so a
    `${VAR}` placeholder is sent to Caddy verbatim and 401s. `{{var}}`
    templating from customUserVars is the only substitution path that works.
    """
    out = render("librechat", ["office"], BASE)
    entry = out["mcpServers"]["office"]
    assert entry["headers"]["Authorization"] == "Bearer {{BEHEROUTER_OFFICE_TOKEN}}"
    assert "BEHEROUTER_OFFICE_TOKEN" in entry["customUserVars"]


def test_librechat_disables_oauth_autodetection():
    """Without this LibreChat shows a "Needs Auth" badge whose button 404s.

    Every surface is hard-401'd by Caddy on every path, with no OAuth
    well-known to discover. LibreChat probes the bare URL with no headers
    before customUserVars exist, sees that 401, and misclassifies the server as
    OAuth-protected. The explicit override is the only thing that stops it —
    detectOAuth() skips entirely once requiresOAuth is non-null.
    """
    out = render("librechat", ["office"], BASE)
    assert out["mcpServers"]["office"]["requiresOAuth"] is False


def test_librechat_customuservar_carries_a_title_and_description():
    """The var is useless without them — LibreChat renders them as the form."""
    out = render("librechat", ["office"], BASE)
    var = out["mcpServers"]["office"]["customUserVars"]["BEHEROUTER_OFFICE_TOKEN"]
    assert var["title"]
    assert var["description"]


def test_hermes_uses_snake_case_mcp_servers_key():
    """Hermes's key is `mcp_servers`, not the camelCase `mcpServers`.

    Verified 2026-08-08 against Hermes Agent v0.19.0 on the hermes VM: this
    exact block in an isolated HERMES_HOME connected in 245ms and discovered 7
    tools, including the gateway-only `search_tools`/`describe_tool`/`run_tool`.
    """
    out = render("hermes", ["office"], BASE)
    assert "mcpServers" not in out
    assert set(out["mcp_servers"]) == {"office"}
    assert out["mcp_servers"]["office"]["url"] == f"{BASE}/office/mcp"


def test_hermes_omits_a_type_key():
    """Hermes infers the transport from `url` — there is no `type` to set.

    `type: http` is tolerated (measured: connects identically) but it is not a
    key Hermes reads, so emitting it would imply a contract that does not exist.
    """
    out = render("hermes", ["office"], BASE)
    assert "type" not in out["mcp_servers"]["office"]


def test_hermes_uses_dollar_brace_from_the_process_environment():
    """`${VAR}` resolves via the profile secret scope, falling back to os.environ.

    That fallback is what lets the harness-wide `BEHEROUTER_<SURFACE>_TOKEN`
    name work. Do NOT provision with `hermes mcp add`: it auto-derives its own
    key `MCP_<NAME>_API_KEY` (hermes_cli/mcp_config.py:153) and writes the token
    into the profile's `.env`, which would fork the harness naming convention.

    An UNSET var keeps the literal placeholder rather than erroring, so the
    whole `Bearer ${...}` string reaches Caddy and 401s — the same asymmetric
    failure as LibreChat. Measured: exit 1, "Client error '401 Unauthorized'".
    """
    out = render("hermes", ["office"], BASE)
    header = out["mcp_servers"]["office"]["headers"]["Authorization"]
    assert header == "Bearer ${BEHEROUTER_OFFICE_TOKEN}"


def test_every_agent_renders_without_error():
    for agent in AGENTS:
        assert render(agent, ["gitea"], BASE)


def test_unknown_agent_raises_usage():
    with pytest.raises(UsageError):
        render("emacs", ["gitea"], BASE)


def test_no_surfaces_still_renders_empty():
    out = render("claude-code", [], BASE)
    assert out["mcpServers"] == {}


def test_token_is_a_placeholder_never_a_real_value():
    """The gateway must never emit a credential — Caddy holds the real tokens."""
    import json

    blob = json.dumps(render("librechat", ["gitea"], BASE))
    assert "BEHEROUTER_GITEA_TOKEN" in blob


def test_default_public_url_is_generic_not_a_real_host():
    """A stranger running client-config must not be pointed at someone's gateway."""
    from beherouter.clientconfig import DEFAULT_PUBLIC_URL, resolve_public_url

    assert DEFAULT_PUBLIC_URL == "http://localhost:47100"
    assert resolve_public_url() == "http://localhost:47100"


def test_public_url_env_override(monkeypatch):
    from beherouter.clientconfig import resolve_public_url

    monkeypatch.setenv("BEHEROUTER_PUBLIC_URL", "https://gw.example.org")
    assert resolve_public_url() == "https://gw.example.org"


def test_explicit_base_url_beats_the_env_override(monkeypatch):
    """--base-url is the operator's last word."""
    from beherouter.clientconfig import resolve_public_url

    monkeypatch.setenv("BEHEROUTER_PUBLIC_URL", "https://gw.example.org")
    assert resolve_public_url("https://explicit.example.org") == "https://explicit.example.org"


def test_empty_env_override_falls_back_to_the_default(monkeypatch):
    """An unset variable and an empty one must behave identically."""
    from beherouter.clientconfig import resolve_public_url

    monkeypatch.setenv("BEHEROUTER_PUBLIC_URL", "")
    assert resolve_public_url() == "http://localhost:47100"

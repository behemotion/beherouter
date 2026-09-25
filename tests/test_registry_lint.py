import json

import pytest

from beherouter.cli.app import registry_lint
from beherouter.errors import UsageError


def _write(tmp_path, body):
    p = tmp_path / "r.toml"
    p.write_text(body)
    return str(p)


def test_clean_registry_passes(tmp_path):
    registry_lint(path=_write(tmp_path, '[office]\nplugin = "office-mcp"\n'))


def test_unknown_plugin_fails(tmp_path):
    with pytest.raises(UsageError, match="unknown plugin"):
        registry_lint(path=_write(tmp_path, '[x]\nplugin = "nope"\n'))


def test_missing_required_config_fails(tmp_path):
    with pytest.raises(UsageError, match="workspace_slug"):
        registry_lint(path=_write(tmp_path, '[plane]\nplugin = "plane"\n'))


def test_plugin_validator_fires(tmp_path):
    body = (
        '[plane]\nplugin = "plane"\n'
        "  [plane.config]\n"
        '  base_url = "http://plane_api_1:8000"\n'
        '  workspace_slug = "homelab"\n'
        "  [plane.env]\n"
        '  api_key = "${T}"\n'
    )
    with pytest.raises(UsageError, match="underscore"):
        registry_lint(path=_write(tmp_path, body))


def test_unset_placeholder_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    body = (
        '[plane]\nplugin = "plane"\n'
        "  [plane.config]\n"
        '  workspace_slug = "homelab"\n'
        "  [plane.env]\n"
        '  api_key = "${NOPE_TOKEN}"\n'
    )
    with pytest.raises(UsageError, match="NOPE_TOKEN"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_opens_no_socket(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket.socket, "connect", lambda *a, **k: pytest.fail("lint opened a socket")
    )
    registry_lint(path=_write(tmp_path, '[office]\nplugin = "office-mcp"\n'))


def test_the_live_registry_shape_lints_clean(tmp_path, monkeypatch):
    """The exact file the cutover ships. A green suite that never linted THIS
    would not tell us the migration is deployable."""
    monkeypatch.setenv("BEHEROUTER_PLANE_TOKEN", "dummy")
    body = (
        '[office]\nplugin = "office-mcp"\n\n'
        '[plane]\nplugin = "plane"\n'
        "  [plane.config]\n"
        '  workspace_slug = "homelab"\n'
        "  [plane.env]\n"
        '  api_key = "${BEHEROUTER_PLANE_TOKEN}"\n'
    )
    registry_lint(path=_write(tmp_path, body))


def test_lint_refuses_an_identity_mode_on_a_shared_only_gateway(tmp_path, monkeypatch):
    """Checked only where the variable is visible — boot is the authority."""
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    body = '[office]\nplugin = "office-mcp"\n  [office.identity]\n  mode = "bearer"\n'
    with pytest.raises(UsageError, match="BEHEROUTER_AUTH_MODE is 'shared'"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_passes_the_same_entry_on_a_gateway_that_can_verify_a_user(
    tmp_path, monkeypatch
):
    """The refusal above must be about the auth mode, not about the entry."""
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    body = '[office]\nplugin = "office-mcp"\n  [office.identity]\n  mode = "bearer"\n'
    registry_lint(path=_write(tmp_path, body))


def test_lint_refuses_a_lookup_map_that_is_absent(tmp_path, monkeypatch):
    """Deferred here from the identity-validation task: `gcal` is the only
    plugin declaring mode `lookup`, so this rule cannot be exercised earlier.
    """
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    body = (
        '[gcal]\nplugin = "gcal"\n'
        "  [gcal.env]\n"
        '  client_id = "id"\n  client_secret = "s"\n  refresh_token = "rt"\n'
        "  [gcal.identity]\n"
        '  mode = "lookup"\n  key = "email"\n'
        f'  path = "{tmp_path / "absent.toml"}"\n'
        "    [gcal.identity.map]\n"
        '    refresh_token = "refresh_token"\n'
    )
    with pytest.raises(UsageError, match="identity map"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_accepts_a_lookup_map_that_is_present(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    map_path = tmp_path / "identity-map.toml"
    map_path.write_text('["alice@example.test"]\nrefresh_token = "rt-alice"\n')
    body = (
        '[gcal]\nplugin = "gcal"\n'
        "  [gcal.env]\n"
        '  client_id = "id"\n  client_secret = "s"\n  refresh_token = "rt"\n'
        "  [gcal.identity]\n"
        '  mode = "lookup"\n  key = "email"\n'
        f'  path = "{map_path}"\n'
        "    [gcal.identity.map]\n"
        '    refresh_token = "refresh_token"\n'
    )
    registry_lint(path=_write(tmp_path, body))


def test_lint_refuses_a_role_gate_on_a_shared_only_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "realm_access.roles")
    body = '[office]\nplugin = "office-mcp"\n  [office.authz]\n  require_roles = ["a"]\n'
    with pytest.raises(UsageError, match="requires a verified user"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_refuses_a_role_gate_with_no_claim_path_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.delenv("BEHEROUTER_OIDC_ROLES_CLAIM", raising=False)
    body = '[office]\nplugin = "office-mcp"\n  [office.authz]\n  require_roles = ["a"]\n'
    with pytest.raises(UsageError, match="BEHEROUTER_OIDC_ROLES_CLAIM"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_accepts_a_role_gate_on_a_stdio_surface(tmp_path, monkeypatch):
    """stdio cannot carry an identity, but it can be gated — nothing is forwarded."""
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "realm_access.roles")
    body = (
        '[plane]\nplugin = "plane"\n'
        "  [plane.config]\n  workspace_slug = \"homelab\"\n"
        "  [plane.env]\n  api_key = \"pat\"\n"
        '  [plane.authz]\n  require_roles = ["ai-plane-access"]\n'
    )
    registry_lint(path=_write(tmp_path, body))


def test_lint_refuses_a_malformed_authz_table(tmp_path):
    body = '[office]\nplugin = "office-mcp"\n  [office.authz]\n  require_roles = "a"\n'
    with pytest.raises(UsageError, match="require_roles"):
        registry_lint(path=_write(tmp_path, body))


def test_a_shared_gateway_is_reported_before_the_missing_claim_path(
    tmp_path, monkeypatch
):
    """Same order as boot: the mode is the cause, the claim path is a symptom.

    `gateway.build_gateway_app` refuses a shared-mode gateway first and only
    then complains about the roles claim. Lint reporting them the other way
    round sends an operator to configure a claim path they do not need yet.
    """
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    monkeypatch.delenv("BEHEROUTER_OIDC_ROLES_CLAIM", raising=False)
    body = '[office]\nplugin = "office-mcp"\n  [office.authz]\n  require_roles = ["a"]\n'
    with pytest.raises(UsageError, match="requires a verified user"):
        registry_lint(path=_write(tmp_path, body))


def test_lint_warns_when_a_backend_credential_uses_the_client_bearer_name(
    tmp_path, monkeypatch, capsys
):
    """`BEHEROUTER_<SURFACE>_TOKEN` is the CLIENT's gateway bearer.

    `clientconfig` builds that exact name for the token a client presents to
    the gateway, so pointing a backend credential at it puts two unrelated
    secrets under one name. Cross-wiring them yields a 401 with nothing to
    point at — which is why this is worth a line of output rather than a
    comment in a values file. It WARNS rather than refuses: the live homelab
    registry uses the colliding name today, and a refusal would turn a naming
    trap into a dead gateway.
    """
    monkeypatch.setenv("BEHEROUTER_PLANE_TOKEN", "pat")
    body = (
        '[plane]\nplugin = "plane"\n'
        '  [plane.config]\n  workspace_slug = "homelab"\n'
        '  [plane.env]\n  api_key = "${BEHEROUTER_PLANE_TOKEN}"\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert any(
        "BEHEROUTER_PLANE_TOKEN" in w and "BEHEROUTER_PLANE_API_KEY" in w
        for w in out["warnings"]
    )


def test_lint_does_not_warn_about_a_distinct_credential_name(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("BEHEROUTER_PLANE_API_KEY", "pat")
    body = (
        '[plane]\nplugin = "plane"\n'
        '  [plane.config]\n  workspace_slug = "homelab"\n'
        '  [plane.env]\n  api_key = "${BEHEROUTER_PLANE_API_KEY}"\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert not [w for w in warnings if "GATEWAY BEARER" in w]


def test_lint_warns_about_a_stdio_command_missing_here(tmp_path, monkeypatch, capsys):
    """A WARNING, not a refusal: lint also runs on workstations that lack the
    image's binaries. In the serving image it names the attach failure ahead."""
    monkeypatch.setenv("BEHEROUTER_PLANE_API_KEY", "pat")
    body = (
        '[plane]\nplugin = "plane"\n'
        '  [plane.config]\n  workspace_slug = "w"\n'
        '  cmd = "/opt/nowhere/plane-mcp-server stdio"\n'
        '  [plane.env]\n  api_key = "${BEHEROUTER_PLANE_API_KEY}"\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert any("/opt/nowhere/plane-mcp-server" in w and "`cmd`" in w for w in out["warnings"])


def test_lint_is_quiet_about_a_stdio_command_that_exists(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setenv("BEHEROUTER_PLANE_API_KEY", "pat")
    body = (
        '[plane]\nplugin = "plane"\n'
        f'  [plane.config]\n  workspace_slug = "w"\n  cmd = "{sys.executable} -V"\n'
        '  [plane.env]\n  api_key = "${BEHEROUTER_PLANE_API_KEY}"\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []


def test_the_collision_warning_never_suggests_the_colliding_name():
    """A credential literally named `token` makes both rules produce the SAME
    variable. Found by the local e2e stack, where the warning read "use ${X}
    instead of ${X}" — advice that repeats the problem is worse than none.
    """
    from beherouter.pluginconfig import collision_warning

    warning = collision_warning("echo", "token", "${BEHEROUTER_ECHO_TOKEN}")
    assert "${BEHEROUTER_ECHO_TOKEN}, which is" in warning
    assert "${BEHEROUTER_ECHO_BACKEND_TOKEN} instead" in warning


def test_no_warning_for_an_unrelated_variable():
    from beherouter.pluginconfig import collision_warning

    assert collision_warning("plane", "api_key", "${BEHEROUTER_PLANE_API_KEY}") is None


def test_lint_warns_on_an_alias_for_an_unknown_tool(tmp_path, capsys):
    body = (
        '[office]\nplugin = "office-mcp"\n'
        '  [office.search_aliases]\n  discovr = ["find"]\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert any("search_aliases" in w and "discovr" in w for w in out["warnings"])


def test_lint_is_quiet_for_an_alias_on_a_pinned_tool(tmp_path, capsys):
    body = (
        '[office]\nplugin = "office-mcp"\n'
        '  [office.search_aliases]\n  discover = ["find"]\n'
    )
    from beherouter.cli.app import app

    assert app.main(["registry-lint", "--path", _write(tmp_path, body), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert not [w for w in out["warnings"] if "search_aliases" in w]

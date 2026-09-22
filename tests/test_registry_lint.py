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

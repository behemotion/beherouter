import pytest

from beherouter.errors import UsageError
from beherouter.pluginconfig import render


def test_office_needs_no_env_fragment():
    out = render("office", "office-mcp")
    assert out["env"] == ""
    assert 'plugin = "office-mcp"' in out["registry"]


def test_caddy_clause_is_anchored_to_the_surface_path():
    out = render("office", "office-mcp")
    assert "not path_regexp office ^/office/mcp/?$" in out["caddy"]


def test_plane_emits_one_env_line_per_credential():
    out = render("plane", "plane")
    assert "BEHEROUTER_PLANE_API_KEY={{ vault_beherouter_plane_api_key }}" in out["env"]


def test_registry_fragment_references_the_placeholder_not_the_value():
    out = render("plane", "plane")
    assert 'api_key = "${BEHEROUTER_PLANE_API_KEY}"' in out["registry"]


def test_required_config_appears_with_its_doc():
    out = render("plane", "plane")
    assert "workspace_slug" in out["registry"]


def test_no_fragment_ever_contains_a_real_secret(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_PLANE_API_KEY", "supersecret")
    out = render("plane", "plane")
    assert "supersecret" not in "".join(out.values())


def test_unknown_plugin_raises():
    with pytest.raises(UsageError, match="unknown plugin"):
        render("x", "nope")


def test_surface_name_with_a_slash_is_rejected():
    with pytest.raises(UsageError, match="surface"):
        render("a/b", "office-mcp")

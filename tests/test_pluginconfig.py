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
    assert "BEHEROUTER_PLANE_API_KEY=" in out["env"]


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


def test_openapi_fragment_parses_with_the_right_shapes():
    """A plugin whose `probe`/`pinned` are mandatory (`requires_entry`) and
    whose config holds a list must get a block with those keys, typed as the
    lint expects — not `include = ""`, which lint refuses as a string."""
    import tomllib

    block = render("crm", "openapi")["registry"].split("\n", 1)[1]
    crm = tomllib.loads(block)["crm"]
    assert crm["pinned"] == [] and crm["probe"] == ""
    assert crm["config"]["include"] == []
    assert crm["config"]["spec"] == ""


def test_no_plugin_credential_derives_the_gateway_bearer_name():
    """A credential named `token` makes `plugin-config` emit
    BEHEROUTER_<SURFACE>_TOKEN, the client's gateway-bearer variable: two
    secrets, one name. Found twice (plane-http, 2026-09-22; openapi, 2026-09-26)."""
    from beherouter.clientconfig import token_var as client_token_var
    from beherouter.pluginconfig import token_var
    from beherouter.plugins import PLUGINS

    clashing = {
        (name, v.name)
        for name, p in PLUGINS.items()
        for v in p.spec.env
        if token_var("s", v.name) == client_token_var("s")
    }
    assert clashing == set()


def test_env_fragment_is_plain_for_any_secret_store():
    """No templating syntax: the deployment may be a .env file, a Kubernetes
    Secret or a vault render. The value is EMPTY, so a forgotten fill refuses
    boot by name instead of booting into 401s."""
    from beherouter.plugins import PLUGINS

    for name, plugin in PLUGINS.items():
        if not plugin.spec.env:
            continue
        lines = render("s", name)["env"].splitlines()[1:]
        assert lines, name
        for line in lines:
            assert "{{" not in line and "}}" not in line, (name, line)
            var = line.lstrip("# ").split("=", 1)
            assert var[0].startswith("BEHEROUTER_S_"), (name, line)
            assert var[1].strip() in ("", "(optional)"), (name, line)

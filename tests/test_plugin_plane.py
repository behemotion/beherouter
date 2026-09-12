import pytest

from beherouter.errors import UsageError
from beherouter.plugins import get
from beherouter.plugins.spec import PluginContext
from beherouter.plugins.validate import validate_config

PLUGIN = "plane"

# Probed live 2026-09-08: every one a 404 from Plane's REST API itself, not from
# the MCP layer. This Plane is the Community Edition and does not serve them.
COMMUNITY_EDITION_GAPS = {
    "page",
    "work_log",
    "milestone",
    "workitem_type",
    "initiative",
}


def test_registered_as_stdio():
    assert get(PLUGIN).spec.backing == "stdio"


def test_pins_eleven_of_thirty():
    assert len(get(PLUGIN).spec.pinned) == 11


def test_plane_pins_exclude_community_edition_gaps():
    """A pinned 404 is a guaranteed dead end the model still spends context on."""
    assert COMMUNITY_EDITION_GAPS.isdisjoint(get(PLUGIN).spec.pinned)


def test_plane_does_not_pin_get_pql_reference():
    """Uncallable upstream in 0.3.2: schema declares `detail`, dispatcher demands
    `action`, and no argument set succeeds. run_tool cannot reach it either."""
    assert "get_pql_reference" not in get(PLUGIN).spec.pinned


def test_probe_authenticates_rather_than_lists():
    spec = get(PLUGIN).spec
    assert spec.probe == "member"
    assert spec.probe_args == {"action": "me"}


def test_workspace_slug_is_required():
    with pytest.raises(UsageError, match="workspace_slug"):
        validate_config("plane", get(PLUGIN).spec, {})


def test_base_url_default_targets_the_network_alias():
    cfg = validate_config("plane", get(PLUGIN).spec, {"workspace_slug": "homelab"})
    assert cfg["base_url"] == "http://plane-api:8000"


def test_base_url_with_an_underscore_in_the_host_is_rejected():
    """Django rejects a Host header containing an underscore with a bare 400,
    BEFORE ALLOWED_HOSTS is consulted. podman-compose names the container
    plane_api_1, so the obvious value is the broken one."""
    with pytest.raises(UsageError, match="underscore"):
        get(PLUGIN).validate({"base_url": "http://plane_api_1:8000", "workspace_slug": "h"})


def test_base_url_without_an_underscore_passes():
    get(PLUGIN).validate({"base_url": "http://plane-api:8000", "workspace_slug": "h"})


def test_underscore_in_a_path_is_allowed():
    """The rule is about the HOST, not the URL."""
    get(PLUGIN).validate({"base_url": "http://plane-api:8000/a_b", "workspace_slug": "h"})


async def test_build_maps_logical_credential_to_the_backend_variable(monkeypatch):
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return "backend"

    import beherouter.plugins.plane as mod

    monkeypatch.setattr(mod, "load_mcp_backend", fake_load)
    ctx = PluginContext(
        surface="plane",
        config={
            "base_url": "http://plane-api:8000",
            "workspace_slug": "homelab",
            "cmd": "/opt/plane-mcp/bin/plane-mcp-server stdio",
        },
        env={"api_key": "secret-pat"},
        pinned=["workitem"],
    )
    await get(PLUGIN).build(ctx)
    env = seen["backing"].env
    assert env["PLANE_API_KEY"] == "secret-pat"
    assert env["PLANE_BASE_URL"] == "http://plane-api:8000"
    assert env["PLANE_WORKSPACE_SLUG"] == "homelab"
    assert seen["backing"].transport == "stdio"

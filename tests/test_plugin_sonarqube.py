import pytest

from beherouter.errors import UsageError
from beherouter.plugins import get
from beherouter.plugins.spec import PluginContext
from beherouter.plugins.validate import validate_config

PLUGIN = "sonarqube"


def test_registered():
    assert get(PLUGIN).spec.backing == "http"


def test_pins_the_read_only_entry_points():
    assert set(get(PLUGIN).spec.pinned) == {
        "search_my_sonarqube_projects",
        "search_sonar_issues_in_projects",
        "get_project_quality_gate_status",
        "get_component_measures",
        "show_rule",
    }


def test_never_pins_a_write():
    pinned = set(get(PLUGIN).spec.pinned)
    assert not pinned & {"change_sonar_issue_status", "change_security_hotspot_status"}


def test_probe_takes_no_required_arguments():
    spec = get(PLUGIN).spec
    assert spec.probe == "search_my_sonarqube_projects"
    assert spec.probe_args == {"pageSize": 1}


def test_declares_the_user_token():
    assert [e.name for e in get(PLUGIN).spec.env] == ["token"]


def test_base_url_defaults_to_the_shared_network_alias():
    cfg = validate_config("sonarqube", get(PLUGIN).spec, {})
    assert cfg["base_url"] == "http://sonarqube-mcp:8080/mcp"


def test_trailing_slash_is_refused():
    """`/mcp/` is a 404 that reads as an absent container."""
    with pytest.raises(UsageError, match="trailing slash"):
        get(PLUGIN).validate({"base_url": "http://sonarqube-mcp:8080/mcp/"})


def test_non_mcp_path_is_refused():
    with pytest.raises(UsageError, match="not an MCP endpoint"):
        get(PLUGIN).validate({"base_url": "http://sonarqube-mcp:8080/"})


async def test_build_declines_the_backends_output_schema(monkeypatch):
    """sonarqube-mcp's schemas forbid nulls its replies contain; republishing
    them fails every call to four of the five pins."""
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return "backend"

    import beherouter.plugins.sonarqube as mod

    monkeypatch.setattr(mod, "load_mcp_backend", fake_load)
    ctx = PluginContext(
        surface="sonarqube",
        config={"base_url": "http://sonarqube-mcp:8080/mcp"},
        pinned=["search_my_sonarqube_projects"],
        env={"token": "squ_test"},
    )
    assert await get(PLUGIN).build(ctx) == "backend"
    b = seen["backing"]
    assert b.transport == "http"
    assert b.url == "http://sonarqube-mcp:8080/mcp"
    assert b.env == {"authorization": "Bearer squ_test"}
    assert b.republish_output_schema is False

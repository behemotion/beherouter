from beherouter.plugins import get
from beherouter.plugins.validate import validate_config

PLUGIN = "office-mcp"


def test_registered():
    assert get(PLUGIN).spec.backing == "http"


def test_pins_the_four_advertised_tools():
    assert set(get(PLUGIN).spec.pinned) == {
        "discover",
        "invoke",
        "file_from_url",
        "job_status",
    }


def test_probe_carries_arguments():
    """office-mcp has NO zero-argument tool, so a bare probe cannot authenticate."""
    spec = get(PLUGIN).spec
    assert spec.probe == "discover"
    assert spec.probe_args == {"query": "pdf"}


def test_declares_no_credential():
    """office-mcp has no app-level auth; a token here would be a lie."""
    assert get(PLUGIN).spec.env == ()


def test_base_url_defaults_to_the_shared_network_alias():
    cfg = validate_config("office", get(PLUGIN).spec, {})
    assert cfg["base_url"] == "http://office-mcp:8100/mcp/"


async def test_build_produces_an_http_backing(monkeypatch):
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return "backend"

    import beherouter.plugins.office_mcp as mod

    monkeypatch.setattr(mod, "load_mcp_backend", fake_load)
    from beherouter.plugins.spec import PluginContext

    ctx = PluginContext(
        surface="office",
        config={"base_url": "http://office-mcp:8100/mcp/"},
        pinned=["discover"],
    )
    assert await get(PLUGIN).build(ctx) == "backend"
    assert seen["backing"].transport == "http"
    assert seen["backing"].url == "http://office-mcp:8100/mcp/"
    assert seen["backing"].name == "office"
    assert seen["backing"].pinned == ["discover"]

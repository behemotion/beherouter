from prometheus_client.parser import text_string_to_metric_families

from beherouter import metrics


def _value(name, labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_a_call_is_counted_and_timed():
    calls = {"surface": "s1", "tool": "t", "outcome": "ok"}
    before = _value("beherouter_tool_calls_total", calls)
    metrics.observe_call("s1", "t", "ok", 0.2)
    assert _value("beherouter_tool_calls_total", calls) == before + 1
    timed = {"surface": "s1", "tool": "t"}
    assert _value("beherouter_tool_call_duration_seconds_count", timed) >= 1


def test_exposition_parses_and_has_no_created_series():
    metrics.observe_call("s1", "t", "ok", 0.1)
    body, ctype = metrics.render()
    assert ctype.startswith("text/plain")
    names = {f.name for f in text_string_to_metric_families(body.decode())}
    assert {"beherouter_tool_calls", "beherouter_tool_call_duration_seconds"} <= names
    assert b"_created" not in body


def test_surface_up_reads_live_state():
    state = {"up": False}
    metrics.track_surface_up("s2", lambda: state["up"])
    assert _value("beherouter_surface_up", {"surface": "s2"}) == 0.0
    state["up"] = True
    assert _value("beherouter_surface_up", {"surface": "s2"}) == 1.0


def test_sessions_are_omitted_when_the_app_has_no_session_table():
    assert metrics.track_sessions("s3", object()) is False
    labels = {"surface": "s3"}
    assert metrics.REGISTRY.get_sample_value("beherouter_active_sessions", labels) is None


async def test_sessions_are_counted_on_a_real_http_app():
    """Held against the pinned FastMCP: if it stops exposing the session
    table, this fails and the series must be dropped, never faked."""
    from fastmcp import FastMCP

    app = FastMCP("x").http_app(path="/mcp")
    assert metrics.track_sessions("s4", app) is True
    assert _value("beherouter_active_sessions", {"surface": "s4"}) == 0.0


async def test_sessions_read_the_manager_the_lifespan_creates():
    from fastmcp import FastMCP

    app = FastMCP("x").http_app(path="/mcp")
    assert metrics.track_sessions("s5", app) is True
    async with app.router.lifespan_context(app):
        holder = metrics._session_holder(app)
        assert isinstance(holder.session_manager._server_instances, dict)
        assert _value("beherouter_active_sessions", {"surface": "s5"}) == 0.0


async def test_sessions_gauge_reads_the_live_table():
    from fastmcp import FastMCP

    app = FastMCP("x").http_app(path="/mcp")
    assert metrics.track_sessions("s6", app) is True
    async with app.router.lifespan_context(app):
        table = metrics._session_holder(app).session_manager._server_instances
        table["dummy"] = object()
        try:
            assert _value("beherouter_active_sessions", {"surface": "s6"}) == 1.0
        finally:
            del table["dummy"]
        assert _value("beherouter_active_sessions", {"surface": "s6"}) == 0.0


async def test_sessions_gauge_skips_a_terminated_session():
    """The MCP SDK keeps a DELETE-terminated transport in its table
    (`is_terminated=True`); it is not an open session."""
    from types import SimpleNamespace

    from fastmcp import FastMCP

    app = FastMCP("x").http_app(path="/mcp")
    assert metrics.track_sessions("s7", app) is True
    async with app.router.lifespan_context(app):
        table = metrics._session_holder(app).session_manager._server_instances
        table["live"] = SimpleNamespace(is_terminated=False)
        table["gone"] = SimpleNamespace(is_terminated=True)
        try:
            assert _value("beherouter_active_sessions", {"surface": "s7"}) == 1.0
        finally:
            del table["live"], table["gone"]


# --- gateway wiring -------------------------------------------------------------


async def test_the_gateway_exposes_surface_up_for_attached_and_failed(monkeypatch):
    import httpx

    from beherouter.gateway import build_gateway_app
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    registry = {
        "deadsurf": RegistryEntry(
            name="deadsurf", plugin="mcp-http",
            config={"url": "http://127.0.0.1:1/mcp"}, pinned=["x"], probe="x",
        ),
    }
    app = await build_gateway_app(registry, retry_initial_s=3600)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        app.router.lifespan_context(app),
    ):
        text = (await c.get("/metrics")).text
    assert 'beherouter_surface_up{surface="deadsurf"} 0.0' in text

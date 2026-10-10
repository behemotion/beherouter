"""A3: a stateless surface serves MCP with no session (spec 2026-10-09)."""

import pytest
from reload_harness import running, write

from beherouter import metrics

TOKEN = {"Authorization": "Bearer s3cret"}
MCP = {**TOKEN, "Accept": "application/json, text/event-stream"}
INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", "0")


def _sessions(name):
    return metrics.REGISTRY.get_sample_value("beherouter_active_sessions", {"surface": name})


async def test_a_stateless_surface_answers_without_a_session(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\nstateless = true\n')
    async with running(reg) as (_rt, c):
        init = await c.post("/a/mcp", json=INIT, headers=MCP)
        assert init.status_code == 200, init.text
        assert "mcp-session-id" not in init.headers
        # every later request is self-contained: no session id is sent
        listed = await c.post(
            "/a/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP
        )
        assert listed.status_code == 200, listed.text
        assert '"search_tools"' in listed.text
        assert "mcp-session-id" not in listed.headers
        called = await c.post(
            "/a/mcp",
            json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search_tools", "arguments": {"query": "x"}},
            },
            headers=MCP,
        )
        assert called.status_code == 200, called.text
        assert '"result"' in called.text
        # no SSE stream to open: FastMCP's stateless app serves POST/DELETE only
        assert (await c.get("/a/mcp", headers=MCP)).status_code == 405


async def test_a_stateful_surface_still_issues_a_session(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (_rt, c):
        init = await c.post("/a/mcp", json=INIT, headers=MCP)
        assert init.status_code == 200, init.text
        assert init.headers["mcp-session-id"]


async def test_a_stateless_surface_has_no_session_gauge(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[full]\nplugin = "t-ok"\n[bare]\nplugin = "t-ok"\nstateless = true\n')
    async with running(reg):
        assert _sessions("full") == 0.0
        assert _sessions("bare") is None


async def test_a_reload_flipping_stateless_reattaches_only_that_surface(
    tmp_path, gateway_plugin, exec_builder
):
    execs: list = []
    gateway_plugin("t-ok", exec_builder(execs))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, c):
        app_a = rt.table.get("a").app
        assert _sessions("b") == 0.0
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\nstateless = true\n')
        result = await rt.reloader.request("admin")
        assert (result["outcome"], result["changed"]) == ("ok", ["b"])
        assert rt.table.get("a").app is app_a
        assert _sessions("b") is None  # the stateful series went with the old app
        init = await c.post("/b/mcp", json=INIT, headers=MCP)
        assert init.status_code == 200 and "mcp-session-id" not in init.headers
        # and back: the series returns
        write(reg, '[a]\nplugin = "t-ok"\n[b]\nplugin = "t-ok"\n')
        assert (await rt.reloader.request("admin"))["changed"] == ["b"]
        assert _sessions("b") == 0.0


async def test_a_reload_refuses_stateless_with_confirm_mutating(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (rt, _c):
        write(
            reg,
            '[a]\nplugin = "t-ok"\nstateless = true\n'
            "  [a.authz]\n  confirm_mutating = true\n",
        )
        result = await rt.reloader.request("admin")
        assert result["outcome"] == "failed"
        assert "needs a session" in result["error"]


async def test_a_stateless_surface_attached_by_the_retry_path_stays_stateless(
    tmp_path, gateway_plugin, exec_builder
):
    import asyncio

    import httpx

    from beherouter.errors import Unavailable
    from beherouter.gateway import build_gateway_app

    calls = {"n": 0}
    good = exec_builder([])

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable("not yet")
        return await good(ctx)

    gateway_plugin("t-flaky", flaky)
    reg = tmp_path / "r.toml"
    write(reg, '[late]\nplugin = "t-flaky"\nstateless = true\n')
    app = await build_gateway_app(reg, retry_initial_s=0.01, retry_max_s=0.05)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        for _ in range(200):
            up = metrics.REGISTRY.get_sample_value("beherouter_surface_up", {"surface": "late"})
            if up == 1.0:
                break
            await asyncio.sleep(0.02)
        assert calls["n"] >= 2
        init = await c.post("/late/mcp", json=INIT, headers=MCP)
        assert init.status_code == 200, init.text
        assert "mcp-session-id" not in init.headers
        assert _sessions("late") is None

import httpx
import pytest
from fastmcp import Client

from beherouter.gateway import DEFAULT_PORT, build_gateway_app, build_surfaces
from beherouter.registry import RegistryEntry


@pytest.fixture
def registry(fake_cli_cmd):
    return {
        "faketool": RegistryEntry(
            name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
        )
    }


async def test_build_surfaces_mounts_each_tool(registry):
    surfaces = await build_surfaces(registry)
    assert "faketool" in surfaces
    async with Client(surfaces["faketool"]) as c:
        names = {t.name for t in await c.list_tools()}
    assert "search_tools" in names
    assert "faketool_search" in names


async def test_default_port_is_47100():
    assert DEFAULT_PORT == 47100


async def test_gateway_app_mounts_surface_paths(registry, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app(registry)
    paths = {r.path for r in app.routes}
    assert "/faketool" in paths


async def test_gateway_rejects_missing_token(registry, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
            r = await c.post(
                "/faketool/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
    assert r.status_code == 401


async def test_gateway_rejects_wrong_token(registry, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
            r = await c.post(
                "/faketool/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    "Authorization": "Bearer wrong",
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert r.status_code == 401


async def test_gateway_refuses_to_start_without_token(registry, monkeypatch):
    """Fail closed: no shared token means no gateway, not an open gateway."""
    monkeypatch.delenv("BEHEROUTER_GATEWAY_TOKEN", raising=False)
    from beherouter.errors import UsageError

    with pytest.raises(UsageError):
        await build_gateway_app(registry)


# --- /healthz ---------------------------------------------------------------
#
# CONVENTIONS.md §Health mandates an UNAUTHENTICATED `GET /healthz`. It is also
# what makes external monitoring meaningful: every other route is bearer-gated,
# so a probe against one of them measures only the edge proxy's own 401 and
# stays green while the gateway is dead.


async def test_healthz_is_unauthenticated_and_ok(registry, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_healthz_reports_attached_surfaces(registry, monkeypatch):
    """Proves the registry actually loaded, not merely that a process is listening."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        r = await c.get("/healthz")
    assert r.json()["surfaces"] == ["faketool"]


# --- empty registry ---------------------------------------------------------
#
# Zero surfaces is a valid operating state — a deployment parked between
# backends — not a boot failure. Refusing to start would take /healthz down with
# it and turn an intentional emptying into a monitoring alarm, which is exactly
# backwards: the gateway is up, it simply fronts nothing.


def _empty_registry_file(tmp_path):
    path = tmp_path / "registry.toml"
    path.write_text("")
    return path


def test_serve_accepts_an_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    import uvicorn

    from beherouter.gateway import serve

    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))

    serve(_empty_registry_file(tmp_path), host="127.0.0.1", port=47100)

    assert served["app"] is not None
    assert served["port"] == 47100


def test_serve_still_refuses_an_empty_registry_without_a_token(tmp_path, monkeypatch):
    """Empty is permitted; unauthenticated is not. Two different failures, and
    relaxing the first must not relax the second."""
    monkeypatch.delenv("BEHEROUTER_GATEWAY_TOKEN", raising=False)
    from beherouter.errors import UsageError
    from beherouter.gateway import serve

    with pytest.raises(UsageError, match="BEHEROUTER_GATEWAY_TOKEN"):
        serve(_empty_registry_file(tmp_path))


async def test_healthz_reports_no_surfaces_when_registry_is_empty(monkeypatch):
    """The empty state must still answer the probe, and say plainly that it is empty."""
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app({})
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "surfaces": []}


# --- Task 6: instructions carry the cost ------------------------------------


async def test_surfaces_carry_cost_instructions(fake_cli_cmd, monkeypatch):
    """A host learns the cost at initialize, with no tool call at all."""
    from beherouter.gateway import build_surfaces
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_TEST_PLUGINS", "1")
    entry = RegistryEntry(
        name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
    )
    surfaces = await build_surfaces({"faketool": entry})
    instructions = surfaces["faketool"].instructions
    assert "faketool" in instructions
    assert "tokens" in instructions
    assert "estimate/json-" in instructions


async def test_costing_failure_does_not_crash_attach_or_other_surfaces(
    fake_cli_cmd, monkeypatch, caplog
):
    """A bug in cost computation must degrade one surface, not the attach.

    `naive_tokens` re-registers EVERY descriptor (pinned or not) through the
    same signature-synthesis path a call-time `context_cost` failure would
    otherwise isolate to one tool call. Run unconditionally at attach, a
    schema edge case there must not crash-loop the whole gateway (AGENTS.md).
    """
    from beherouter import costing
    from beherouter.gateway import build_surfaces
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_TEST_PLUGINS", "1")
    real_surface_cost = costing.surface_cost

    async def flaky(mcp, backend):
        if backend.name == "broken":
            raise RuntimeError("boom")
        return await real_surface_cost(mcp, backend)

    monkeypatch.setattr(costing, "surface_cost", flaky)

    registry = {
        "broken": RegistryEntry(
            name="broken", plugin="_test-cli", config={"cmd": fake_cli_cmd}
        ),
        "faketool": RegistryEntry(
            name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
        ),
    }
    with caplog.at_level("WARNING"):
        surfaces = await build_surfaces(registry)

    # The attach itself must survive -- both surfaces present.
    assert set(surfaces) == {"broken", "faketool"}

    # The broken surface degrades to no cost instructions rather than raising.
    assert surfaces["broken"].instructions is None

    # The other surface, in the SAME registry, is unaffected.
    assert surfaces["faketool"].instructions is not None
    assert "faketool" in surfaces["faketool"].instructions

    assert any("broken" in r.message for r in caplog.records)


# --- load_backend: catalogue_ttl_ms resolution (entry overrides plugin) -----
#
# This is the exact chain the Catalogue trap runs through: `Catalogue` gates
# refresh on `relist is not None and bool(ttl_ms)`, so if this resolution ever
# silently handed back None or 0, every mcp surface would stop refreshing with
# no exception and no log line. The resolution itself is not mcp-specific --
# load_backend reads the entry and the plugin spec regardless of backing -- so
# `_test-cli` is the cheapest vehicle and needs no credentials.


async def test_load_backend_honors_the_entry_ttl_override(fake_cli_cmd):
    from beherouter.gateway import load_backend

    backend = await load_backend(
        RegistryEntry(
            name="faketool",
            plugin="_test-cli",
            config={"cmd": fake_cli_cmd},
            catalogue_ttl_ms=60_000,
        )
    )
    assert backend.ttl_ms == 60_000


async def test_load_backend_falls_back_to_the_plugin_default_ttl(fake_cli_cmd):
    from beherouter.gateway import load_backend
    from beherouter.plugins.spec import PluginSpec

    # Pins the constant itself: a change to PluginSpec's default must be visible
    # here rather than only inferred from the assertion below.
    assert PluginSpec(name="x", summary="", backing="cli").catalogue_ttl_ms == 300_000

    backend = await load_backend(
        RegistryEntry(name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd})
    )
    # No override in the entry -> the plugin's tested default, exactly like
    # `pinned` and `probe`.
    assert backend.ttl_ms == 300_000
    # ACTUAL, possibly-surprising behaviour: load_backend assigns ttl_ms
    # unconditionally, regardless of backing -- a `cli` backend gets a TTL it
    # can never use because it has no relister. The TTL is a gateway concern
    # resolved once for every backend; only Catalogue's `_enabled` gate (relist
    # is not None and bool(ttl_ms)) makes that harmless for `cli`/`native`.
    assert backend.relist is None


async def test_a_native_backend_has_no_relister_either(monkeypatch):
    """The TTL constraint names `cli` AND `native`; only `cli` was covered.

    Structurally guaranteed by `Backend.relist`'s default rather than by
    anything in the calendar plugins, which is exactly why nothing would catch
    a plugin that started setting it.
    """
    from beherouter.gateway import load_backend

    monkeypatch.setenv("BEHE_T_GCAL_CID", "cid")
    monkeypatch.setenv("BEHE_T_GCAL_SEC", "sec")
    monkeypatch.setenv("BEHE_T_GCAL_RT", "rt")
    backend = await load_backend(
        RegistryEntry(
            name="gcal",
            plugin="gcal",
            env={
                "client_id": "${BEHE_T_GCAL_CID}",
                "client_secret": "${BEHE_T_GCAL_SEC}",
                "refresh_token": "${BEHE_T_GCAL_RT}",
            },
        )
    )
    assert backend.kind == "native"
    assert backend.relist is None


async def test_build_gateway_app_refuses_per_user_on_a_shared_gateway(monkeypatch):
    from beherouter.errors import UsageError
    from beherouter.gateway import build_gateway_app
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    registry = {
        "office": RegistryEntry(
            name="office", plugin="office-mcp", identity={"mode": "bearer"}
        )
    }
    with pytest.raises(UsageError, match="BEHEROUTER_AUTH_MODE"):
        await build_gateway_app(registry)


async def test_build_gateway_app_refuses_a_role_gate_on_a_shared_gateway(monkeypatch):
    from beherouter.errors import UsageError
    from beherouter.gateway import build_gateway_app
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    registry = {
        "office": RegistryEntry(
            name="office", plugin="office-mcp", authz={"require_roles": ["a"]}
        )
    }
    with pytest.raises(UsageError, match="require a verified user"):
        await build_gateway_app(registry)


async def test_build_gateway_app_refuses_a_role_gate_with_no_claim_path(monkeypatch):
    """A gate with nowhere to read roles from can only fail every call."""
    from beherouter.errors import UsageError
    from beherouter.gateway import build_gateway_app
    from beherouter.registry import RegistryEntry

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("BEHEROUTER_OIDC_AUDIENCE", "beherouter")
    monkeypatch.setenv("BEHEROUTER_OIDC_JWKS_URI", "https://idp.test/jwks")
    monkeypatch.delenv("BEHEROUTER_OIDC_ROLES_CLAIM", raising=False)
    registry = {
        "office": RegistryEntry(
            name="office", plugin="office-mcp", authz={"require_roles": ["a"]}
        )
    }
    with pytest.raises(UsageError, match="BEHEROUTER_OIDC_ROLES_CLAIM"):
        await build_gateway_app(registry)


async def test_load_backend_resolves_registry_search_aliases(fake_cli_cmd):
    """Resolved beside ttl_ms, so no plugin's build() has to forward it."""
    from beherouter.gateway import load_backend

    backend = await load_backend(
        RegistryEntry(
            name="faketool",
            plugin="_test-cli",
            config={"cmd": fake_cli_cmd},
            search_aliases={"faketool_search": ["lookup"]},
        )
    )
    assert backend.search_aliases == {"faketool_search": ("lookup",)}

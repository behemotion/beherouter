"""A role- or audience-gated surface hides its tools from `tools/list`.

The gate on CALLS has existed since the identity work; this is the gate on
ENUMERATION. An MCP host puts every listed tool in front of the model for every
user, so a surface that refuses a caller's calls but lists its tools to them
teaches their model to offer tools that always fail.

Rules held here (from a production deployment's request, 2026-10-06):

- refused caller -> `{"tools": []}`, never an error: an error in `tools/list`
  makes most hosts mark the whole server failed;
- `tools/call` is unchanged and still the real gate, naming the missing role;
- the listing gates WITHOUT materialising, like `guard()`;
- a mode-only surface (no role, no audience) is NOT hidden;
- `[surface.authz] hide_tools = false` restores the old behaviour.
"""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import beherouter.identity as identity_mod
from beherouter.costing import surface_cost
from beherouter.errors import UsageError
from beherouter.identity import (
    IdentityPolicy,
    RequestIdentity,
    identity_report,
    policy_from_entry,
    validate_authz,
)
from beherouter.models import Backend, ToolDescriptor
from beherouter.plugins.spec import PluginSpec
from beherouter.registry import RegistryEntry
from beherouter.surface import build_surface

META = {"search_tools", "describe_tool", "run_tool", "context_cost"}


class SpyExecutor:
    def __init__(self):
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append((verb, args, identity))
        return {"result": "ok"}


def _backend(spy):
    d = ToolDescriptor(
        name="run_query",
        verb="run_query",
        summary="run a read-only query",
        schema={},
        pinned=True,
        mutating=False,
    )
    return Backend(name="dwh", kind="mcp", descriptors=[d], executor=spy)


def _caller(monkeypatch, *, roles=None, shared=False, aud=None):
    """Make the live request this caller. Exercises the REAL gate()."""
    claims = {"sub": "alice"}
    if roles is not None:
        claims["realm_access"] = {"roles": list(roles)}
    if aud is not None:
        claims["aud"] = aud
    req = RequestIdentity(
        shared=shared,
        subject=None if shared else "alice",
        claims={} if shared else claims,
        raw_token="t",
    )
    monkeypatch.setattr(identity_mod, "request_identity", lambda wanted=(): req)


def _roles_gate(**kw) -> IdentityPolicy:
    return IdentityPolicy(
        surface="dwh",
        require_roles=("ai-dwh-access",),
        roles_claim="realm_access.roles",
        **kw,
    )


async def _listed(surface) -> set[str]:
    async with Client(surface) as c:
        return {t.name for t in await c.list_tools()}


# --- the acceptance table ---------------------------------------------------


async def test_a_caller_without_the_role_lists_nothing(monkeypatch):
    _caller(monkeypatch, roles=["something-else"])
    assert await _listed(build_surface(_backend(SpyExecutor()), policy=_roles_gate())) == set()


async def test_a_caller_holding_the_role_lists_the_published_array(monkeypatch):
    _caller(monkeypatch, roles=["ai-dwh-access"])
    surface = build_surface(_backend(SpyExecutor()), policy=_roles_gate())
    assert await _listed(surface) == {"run_query"} | META


async def test_a_shared_token_caller_lists_nothing(monkeypatch):
    """Under `auth.mode: both` the shared token fails gate() — same as a miss."""
    _caller(monkeypatch, shared=True)
    assert await _listed(build_surface(_backend(SpyExecutor()), policy=_roles_gate())) == set()


async def test_an_audience_gate_hides_too(monkeypatch):
    _caller(monkeypatch, aud="another-surface")
    policy = IdentityPolicy(surface="dwh", audiences=("dwh",))
    assert await _listed(build_surface(_backend(SpyExecutor()), policy=policy)) == set()


async def test_a_hidden_tool_still_refuses_its_call_by_role_name(monkeypatch):
    """Hiding is cosmetic; the call is still the gate, and still says why."""
    _caller(monkeypatch, roles=[])
    spy = SpyExecutor()
    surface = build_surface(_backend(spy), policy=_roles_gate())
    async with Client(surface) as c:
        for tool, args in (("run_query", {}), ("search_tools", {"query": "q"})):
            with pytest.raises(ToolError, match="ai-dwh-access"):
                await c.call_tool(tool, args)
    assert spy.calls == []


async def test_initialize_still_succeeds_for_a_refused_caller(monkeypatch):
    """An empty server is the honest state; a failed connect looks broken."""
    _caller(monkeypatch, roles=[])
    surface = build_surface(_backend(SpyExecutor()), policy=_roles_gate())
    async with Client(surface) as c:
        assert c.initialize_result is not None


# --- what must stay unaffected ----------------------------------------------


async def test_an_ungated_surface_lists_with_no_verified_caller():
    """No policy: no request is read at all — the in-process client has none."""
    surface = build_surface(_backend(SpyExecutor()))
    assert await _listed(surface) == {"run_query"} | META


async def test_a_mode_only_surface_is_not_hidden(monkeypatch):
    """A mode says WHOSE credential, not WHO MAY; only a gate hides."""
    _caller(monkeypatch, shared=True)
    policy = IdentityPolicy(surface="dwh", mode="bearer", target="header")
    assert await _listed(build_surface(_backend(SpyExecutor()), policy=policy)) == {
        "run_query"
    } | META


async def test_hide_tools_false_restores_the_full_listing(monkeypatch):
    _caller(monkeypatch, roles=[])
    surface = build_surface(_backend(SpyExecutor()), policy=_roles_gate(hide_tools=False))
    assert await _listed(surface) == {"run_query"} | META


async def test_the_listing_never_materialises(monkeypatch):
    """Same reasoning as guard(): enumeration must not depend on a readable
    identity map, so a lookup surface with a broken map still lists."""
    _caller(monkeypatch, roles=["ai-dwh-access"])
    policy = _roles_gate(mode="lookup", target="credential", path="/nonexistent/map.toml")
    surface = build_surface(_backend(SpyExecutor()), policy=policy)
    assert await _listed(surface) == {"run_query"} | META


async def test_a_misconfigured_gate_lists_nothing_rather_than_raising(monkeypatch):
    """No roles claim configured is a UsageError in gate(); in tools/list it
    must fail CLOSED and quietly — an error there kills the whole server."""
    _caller(monkeypatch, roles=["ai-dwh-access"])
    policy = IdentityPolicy(surface="dwh", require_roles=("ai-dwh-access",))
    assert await _listed(build_surface(_backend(SpyExecutor()), policy=policy)) == set()


async def test_the_cost_measures_the_published_array_with_no_caller():
    """costing runs at attach and from the CLI, where no request exists. It
    must measure what is published, not what an absent caller would see."""
    backend = _backend(SpyExecutor())
    cost = await surface_cost(build_surface(backend, policy=_roles_gate()), backend)
    assert cost.tokens_pinned > 0 and cost.tokens_meta > 0


# --- the registry switch ----------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_hide_tools_is_a_valid_authz_key_beside_a_gate(value):
    validate_authz("dwh", {"require_roles": ["r"], "hide_tools": value})
    validate_authz("dwh", {"audience": "dwh", "hide_tools": value})


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_hide_tools_must_be_a_boolean(value):
    with pytest.raises(UsageError, match="hide_tools"):
        validate_authz("dwh", {"require_roles": ["r"], "hide_tools": value})


def test_hide_tools_without_a_gate_is_refused():
    with pytest.raises(UsageError, match="hide_tools"):
        validate_authz("dwh", {"hide_tools": True})


def test_policy_from_entry_defaults_to_hiding(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "realm_access.roles")
    spec = PluginSpec(name="demo", summary="demo", backing="stdio")
    on = RegistryEntry(name="dwh", plugin="demo", authz={"require_roles": ["r"]})
    off = RegistryEntry(
        name="dwh", plugin="demo", authz={"require_roles": ["r"], "hide_tools": False}
    )
    assert policy_from_entry(on, spec).hides_listing is True
    assert policy_from_entry(off, spec).hides_listing is False


def test_the_identity_report_says_whether_the_listing_is_hidden():
    assert identity_report(_roles_gate())["hide_tools"] is True
    assert identity_report(_roles_gate(hide_tools=False))["hide_tools"] is False
    assert "hide_tools" not in identity_report(IdentityPolicy(surface="dwh"))


# --- over the wire: the acceptance table, verbatim --------------------------
#
# The tests above fake the request; this one does not. A real RS256 JWT goes
# through the gateway's own composite verifier (`auth.mode: both`) over
# streamable HTTP, so the middleware is proven where the host meets it.


async def test_the_acceptance_table_over_streamable_http():
    import httpx
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    from beherouter.auth import (
        CompositeVerifier,
        GatewayJWTVerifier,
        SharedTokenVerifier,
    )

    kp = RSAKeyPair.generate()
    auth = CompositeVerifier(
        [
            SharedTokenVerifier("s3cret"),
            GatewayJWTVerifier(
                public_key=kp.public_key, issuer="https://idp.test", audience="gw"
            ),
        ]
    )

    def jwt(roles):
        return kp.create_token(
            subject="alice",
            issuer="https://idp.test",
            audience="gw",
            additional_claims={"realm_access": {"roles": roles}},
        )

    spy = SpyExecutor()
    app = build_surface(_backend(spy), auth=auth, policy=_roles_gate()).http_app(
        path="/mcp"
    )
    async with app.router.lifespan_context(app):
        asgi = httpx.ASGITransport(app=app)

        def client(token):
            return Client(
                StreamableHttpTransport(
                    "http://gw/mcp",
                    auth=token,
                    httpx_client_factory=lambda **kw: httpx.AsyncClient(
                        transport=asgi, **kw
                    ),
                )
            )

        for refused in (jwt(["other"]), "s3cret"):
            async with client(refused) as c:  # initialize -> 200
                assert await c.list_tools() == []
                with pytest.raises(ToolError, match="you do not have access|verified user"):
                    await c.call_tool("run_query", {})
                with pytest.raises(ToolError, match="you do not have access|verified user"):
                    await c.call_tool("search_tools", {"query": "q"})

        async with client(jwt(["ai-dwh-access"])) as c:
            assert {t.name for t in await c.list_tools()} == {"run_query"} | META
            await c.call_tool("run_query", {})
    assert [verb for verb, _, _ in spy.calls] == ["run_query"]

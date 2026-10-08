"""Mode `exchange`: RFC 8693 token exchange, per call, cached, never a fallback.

The token endpoint is an `httpx.MockTransport` routed in through
`tokenexchange.TRANSPORT`; nothing here touches a network.
"""

import asyncio
import base64
import json
import logging
from typing import ClassVar
from urllib.parse import parse_qs

import httpx
import pytest

from beherouter import tokenexchange
from beherouter.errors import AuthError, Unavailable, UsageError
from beherouter.identity import (
    CallIdentity,
    IdentityPolicy,
    RequestIdentity,
    identity_report,
    policy_from_entry,
    settle,
    validate_identity,
)
from beherouter.plugins.spec import EnvVar, IdentitySupport, PluginSpec
from beherouter.registry import RegistryEntry, validate_entry

TOKEN_URL = "https://idp.test/token"
SECRET_VAR = "BEHEROUTER_DEMO_EXCHANGE_SECRET"
SUBJECT = "eyJ.subject-token.sig"

HTTP = PluginSpec(
    name="demo-http",
    summary="demo",
    backing="http",
    identity=IdentitySupport(modes=("bearer", "exchange"), target="header"),
)
CLI = PluginSpec(
    name="demo-cli",
    summary="demo",
    backing="cli",
    identity=IdentitySupport(modes=("claims", "exchange"), target="env"),
)
NATIVE = PluginSpec(
    name="demo-native",
    summary="demo",
    backing="native",
    env=(EnvVar(name="refresh_token"),),
    identity=IdentitySupport(
        modes=("lookup", "exchange"), target="credential", accepts=("refresh_token",)
    ),
)


def _table(**kw) -> dict:
    base = {
        "mode": "exchange",
        "token_url": TOKEN_URL,
        "audience": "crm-api",
        "client_id": "beherouter",
        "client_secret": f"${{{SECRET_VAR}}}",
    }
    return {k: v for k, v in {**base, **kw}.items() if v is not None}


def _user(**kw) -> RequestIdentity:
    base = {
        "shared": False,
        "subject": "alice",
        "claims": {"sub": "alice"},
        "raw_token": SUBJECT,
        "headers": {},
    }
    return RequestIdentity(**{**base, **kw})


def _policy(**kw) -> IdentityPolicy:
    entry = RegistryEntry(name="crm", plugin="x", identity=_table(**kw))
    validate_identity("crm", HTTP, entry.identity)
    return policy_from_entry(entry, HTTP)


class IdP:
    """A token endpoint that records what it was sent."""

    def __init__(self, status=200, body=None, expires_in=300, delay=0.0, raise_exc=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body
        self.expires_in = expires_in
        self.delay = delay
        self.raise_exc = raise_exc
        self.issued = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        self.issued += 1
        body = {
            "access_token": f"exchanged-{self.issued}",
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer",
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(self.status, json=body)

    def form(self, i=-1) -> dict[str, list[str]]:
        return parse_qs(self.requests[i].content.decode())


@pytest.fixture
def idp(monkeypatch):
    server = IdP()
    monkeypatch.setattr(tokenexchange, "TRANSPORT", httpx.MockTransport(server.handler))
    monkeypatch.setenv(SECRET_VAR, "s3cret")
    tokenexchange.reset()
    yield server
    tokenexchange.reset()


# --- offline validation ---------------------------------------------------


def test_a_complete_exchange_table_is_valid():
    validate_identity("crm", HTTP, _table())
    validate_identity("crm", HTTP, _table(audience=None, resource="https://crm.test/"))
    validate_identity(
        "crm",
        HTTP,
        _table(
            audience=["a", "b"],
            scope=["read", "write"],
            subject_token_type="access_token",
            client_auth="client_secret_post",
            client_id="${BEHEROUTER_DEMO_CLIENT_ID}",
            header="x-forwarded-token",
            prefix="",
        ),
    )


@pytest.mark.parametrize(
    "override, match",
    [
        ({"token_url": None}, "token_url"),
        ({"token_url": "idp.test/token"}, "token_url"),
        ({"token_url": "ftp://idp.test/token"}, "token_url"),
        ({"token_url": "https://user:pw@idp.test/token"}, "token_url"),
        ({"token_url": "https://idp.test/token#frag"}, "token_url"),
        ({"audience": None}, "audience"),
        ({"audience": ""}, "audience"),
        ({"audience": ["ok", 3]}, "audience"),
        ({"resource": 7}, "resource"),
        ({"scope": ""}, "scope"),
        ({"client_id": None}, "client_id"),
        ({"client_secret": None}, "client_secret"),
        ({"client_secret": "inline-secret"}, r"\$\{VAR\}"),
        ({"client_secret": "pre-${X}"}, r"\$\{VAR\}"),
        ({"client_auth": "private_key_jwt"}, "client_auth"),
        ({"subject_token_type": "saml2"}, "subject_token_type"),
        ({"map": {"a": "b"}}, "map"),
        ({"path": "/x"}, "path"),
        ({"header": 3}, "header"),
    ],
)
def test_an_incomplete_or_unsafe_exchange_table_is_refused(override, match):
    with pytest.raises(UsageError, match=match):
        validate_identity("crm", HTTP, _table(**override))


def test_an_inline_secret_is_never_echoed_by_the_refusal():
    with pytest.raises(UsageError) as e:
        validate_identity("crm", HTTP, _table(client_secret="hunter2-inline"))
    assert "hunter2" not in str(e.value)


def test_exchange_keys_on_another_mode_are_refused():
    with pytest.raises(UsageError, match="token_url"):
        validate_identity("crm", HTTP, {"mode": "bearer", "token_url": TOKEN_URL})


@pytest.mark.parametrize("spec", [CLI, NATIVE])
def test_exchange_is_refused_off_the_header_target(spec):
    with pytest.raises(UsageError, match="header"):
        validate_identity("crm", spec, _table())


def test_the_registry_path_validates_it_too():
    entry = RegistryEntry(
        name="crm",
        plugin="mcp-http",
        config={"url": "https://crm.test/mcp"},
        probe="ping",
        pinned=["ping"],
        identity=_table(client_secret="inline"),
    )
    with pytest.raises(UsageError, match="client_secret"):
        validate_entry(entry)


@pytest.mark.parametrize("plugin", ["mcp-http", "openapi", "office-mcp", "plane-http"])
def test_the_header_plugins_declare_exchange(plugin):
    from beherouter.plugins import get

    assert "exchange" in get(plugin).spec.identity.modes


@pytest.mark.parametrize(
    "plugin", ["plane", "plane-http-apikey", "gcal", "m365", "sonarqube", "mcp-stdio"]
)
def test_other_plugins_do_not(plugin):
    from beherouter.plugins import get

    assert "exchange" not in get(plugin).spec.identity.modes


# --- the policy and materialisation ---------------------------------------


def test_policy_from_entry_carries_the_exchange_config():
    p = _policy(audience=["a", "b"], scope=["x", "y"], resource="https://r/")
    assert p.mode == "exchange"
    assert p.exchange.token_url == TOKEN_URL
    assert p.exchange.audience == ("a", "b")
    assert p.exchange.resource == ("https://r/",)
    assert p.exchange.scope == "x y"
    assert p.exchange.subject_token_type == "urn:ietf:params:oauth:token-type:jwt"
    assert p.exchange.client_auth == "client_secret_basic"
    assert p.header == "authorization" and p.prefix == "Bearer "


def test_materialise_does_no_io_and_leaves_a_pending_exchange(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("materialise touched the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    ident = _policy().materialise(_user())
    assert ident.headers == {}
    assert ident.pending is not None
    assert ident.subject == "alice"
    assert SUBJECT not in ident.cache_key


def test_exchange_without_a_raw_token_refuses():
    with pytest.raises(AuthError, match="crm"):
        _policy().materialise(_user(raw_token=None))


def test_a_shared_caller_is_refused_before_any_exchange():
    with pytest.raises(AuthError):
        _policy().materialise(_user(shared=True))


def test_materialise_refuses_exchange_off_a_header_target():
    p = IdentityPolicy(surface="crm", mode="exchange", target="env")
    with pytest.raises(UsageError, match="header"):
        p.materialise(_user())


async def test_settle_passes_a_plain_identity_through():
    plain = CallIdentity(subject="a", headers={"x": "y"})
    assert await settle(plain) is plain
    assert await settle(None) is None


# --- the exchange itself ----------------------------------------------------


async def test_the_exchange_request_is_rfc_8693_with_basic_auth(idp):
    ident = await settle(_policy(scope="read write", resource="https://crm/").materialise(_user()))
    assert ident.headers == {"authorization": "Bearer exchanged-1"}
    assert ident.pending is None
    req = idp.requests[0]
    assert req.method == "POST"
    assert str(req.url) == TOKEN_URL
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = idp.form()
    assert form == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
        "subject_token": [SUBJECT],
        "subject_token_type": ["urn:ietf:params:oauth:token-type:jwt"],
        "audience": ["crm-api"],
        "resource": ["https://crm/"],
        "scope": ["read write"],
    }
    basic = base64.b64encode(b"beherouter:s3cret").decode()
    assert req.headers["authorization"] == f"Basic {basic}"


async def test_basic_auth_form_encodes_the_client_credentials(idp, monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "a:b c")
    await settle(_policy(client_id="id:x").materialise(_user()))
    basic = base64.b64encode(b"id%3Ax:a%3Ab+c").decode()
    assert idp.requests[0].headers["authorization"] == f"Basic {basic}"


async def test_client_secret_post_puts_the_credentials_in_the_body(idp):
    await settle(
        _policy(client_auth="client_secret_post", audience=["a", "b"]).materialise(_user())
    )
    form = idp.form()
    assert form["client_id"] == ["beherouter"]
    assert form["client_secret"] == ["s3cret"]
    assert form["audience"] == ["a", "b"]
    assert "authorization" not in idp.requests[0].headers


async def test_access_token_subject_type_and_custom_header(idp, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_DEMO_CLIENT_ID", "from-env")
    ident = await settle(
        _policy(
            subject_token_type="access_token",
            header="x-token",
            prefix="",
            client_id="${BEHEROUTER_DEMO_CLIENT_ID}",
        ).materialise(_user())
    )
    assert ident.headers == {"x-token": "exchanged-1"}
    assert idp.form()["subject_token_type"] == [
        "urn:ietf:params:oauth:token-type:access_token"
    ]
    assert idp.requests[0].headers["authorization"].startswith("Basic ")
    assert base64.b64decode(idp.requests[0].headers["authorization"][6:]).startswith(
        b"from-env:"
    )


# --- caching ----------------------------------------------------------------


async def test_a_second_call_with_the_same_token_is_served_from_cache(idp):
    p = _policy()
    a = await settle(p.materialise(_user()))
    b = await settle(p.materialise(_user()))
    assert a.headers == b.headers == {"authorization": "Bearer exchanged-1"}
    assert len(idp.requests) == 1


async def test_a_different_subject_token_exchanges_again(idp):
    p = _policy()
    await settle(p.materialise(_user()))
    other = await settle(p.materialise(_user(raw_token="eyJ.bob.sig", subject="bob")))
    assert other.headers == {"authorization": "Bearer exchanged-2"}
    assert len(idp.requests) == 2


async def test_the_cache_expires_before_the_token_does(idp, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tokenexchange, "_now", lambda: now[0])
    p = _policy()
    await settle(p.materialise(_user()))
    now[0] += 300 - tokenexchange.SKEW_S - 1
    await settle(p.materialise(_user()))
    assert len(idp.requests) == 1
    now[0] += 2
    again = await settle(p.materialise(_user()))
    assert again.headers == {"authorization": "Bearer exchanged-2"}
    assert len(idp.requests) == 2


@pytest.mark.parametrize("expires_in", [None, 10])
async def test_a_token_without_a_usable_lifetime_is_not_cached(idp, expires_in):
    idp.expires_in = expires_in
    p = _policy()
    await settle(p.materialise(_user()))
    await settle(p.materialise(_user()))
    assert len(idp.requests) == 2


async def test_the_cache_is_bounded(idp, monkeypatch):
    monkeypatch.setattr(tokenexchange, "MAX_ENTRIES", 2)
    tokenexchange.reset()
    p = _policy()
    for t in ("t1", "t2", "t3"):
        await settle(p.materialise(_user(raw_token=t)))
    await settle(p.materialise(_user(raw_token="t3")))  # still cached
    assert len(idp.requests) == 3
    await settle(p.materialise(_user(raw_token="t1")))  # evicted
    assert len(idp.requests) == 4


async def test_the_cache_holds_no_raw_token(idp):
    p = _policy()
    await settle(p.materialise(_user()))
    ex = tokenexchange.exchanger_for(p.surface, p.exchange)
    assert all(SUBJECT not in key for key in ex._cache)


async def test_concurrent_calls_for_one_caller_share_one_exchange(idp):
    idp.delay = 0.05
    p = _policy()
    results = await asyncio.gather(*(settle(p.materialise(_user())) for _ in range(8)))
    assert len(idp.requests) == 1
    assert {r.headers["authorization"] for r in results} == {"Bearer exchanged-1"}


async def test_a_cancelled_waiter_does_not_cancel_the_others(idp):
    idp.delay = 0.05
    p = _policy()
    first = asyncio.ensure_future(settle(p.materialise(_user())))
    second = asyncio.ensure_future(settle(p.materialise(_user())))
    await asyncio.sleep(0.01)
    first.cancel()
    assert (await second).headers == {"authorization": "Bearer exchanged-1"}
    assert len(idp.requests) == 1


async def test_a_changed_configuration_gets_a_fresh_exchanger(idp):
    await settle(_policy().materialise(_user()))
    await settle(_policy(audience="other-api").materialise(_user()))
    assert len(idp.requests) == 2
    assert idp.form()["audience"] == ["other-api"]


# --- failure semantics ------------------------------------------------------


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_target"])
async def test_a_refused_subject_is_an_auth_error_naming_only_the_code(idp, code):
    idp.status, idp.body = 400, {"error": code, "error_description": f"bad {SUBJECT}"}
    with pytest.raises(AuthError) as e:
        await settle(_policy().materialise(_user()))
    msg = str(e.value)
    assert "crm" in msg and code in msg
    assert SUBJECT not in msg and "bad" not in msg


@pytest.mark.parametrize(
    "status, body",
    [
        (400, {"error": "invalid_request"}),
        (401, {"error": "invalid_client"}),
        (400, {"error": "unauthorized_client"}),
        (400, {"error": "invalid_scope"}),
        (400, {"error": "unsupported_grant_type"}),
        (403, {"nothing": "useful"}),
    ],
)
async def test_a_gateway_side_misconfiguration_is_a_usage_error(idp, status, body):
    idp.status, idp.body = status, body
    with pytest.raises(UsageError, match="crm"):
        await settle(_policy().materialise(_user()))


async def test_an_unprintable_error_code_is_not_echoed(idp):
    idp.status, idp.body = 400, {"error": "x\n" + SUBJECT}
    with pytest.raises(UsageError) as e:
        await settle(_policy().materialise(_user()))
    assert SUBJECT not in str(e.value)
    assert "unrecognised" in str(e.value)


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_an_idp_outage_is_unavailable(idp, status):
    idp.status, idp.body = status, {"error": "temporarily_unavailable"}
    with pytest.raises(Unavailable, match="crm"):
        await settle(_policy().materialise(_user()))


async def test_a_network_error_is_unavailable(idp):
    idp.raise_exc = httpx.ConnectError("refused")
    with pytest.raises(Unavailable, match="ConnectError"):
        await settle(_policy().materialise(_user()))


@pytest.mark.parametrize(
    "body", [{"token_type": "Bearer"}, {"access_token": "", "token_type": "Bearer"}]
)
async def test_a_success_without_a_token_is_unavailable(idp, body):
    idp.body = body
    with pytest.raises(Unavailable, match="crm"):
        await settle(_policy().materialise(_user()))


async def test_a_non_json_success_is_unavailable(idp, monkeypatch):
    def text(request):
        return httpx.Response(200, text="<html>login</html>")

    monkeypatch.setattr(tokenexchange, "TRANSPORT", httpx.MockTransport(text))
    with pytest.raises(Unavailable, match="crm"):
        await settle(_policy().materialise(_user()))


async def test_a_non_bearer_token_type_is_refused(idp):
    idp.body = {"access_token": "x", "token_type": "DPoP", "expires_in": 300}
    with pytest.raises(UsageError, match="DPoP"):
        await settle(_policy().materialise(_user()))


async def test_an_unset_secret_degrades_the_call_not_the_gateway(idp, monkeypatch):
    monkeypatch.delenv(SECRET_VAR)
    p = _policy()  # building the policy does not need the secret
    with pytest.raises(Unavailable, match=SECRET_VAR):
        await settle(p.materialise(_user()))
    assert idp.requests == []


async def test_a_failure_is_not_cached(idp):
    idp.status, idp.body = 503, {"error": "x"}
    p = _policy()
    with pytest.raises(Unavailable):
        await settle(p.materialise(_user()))
    idp.status, idp.body = 200, None
    ok = await settle(p.materialise(_user()))
    assert ok.headers == {"authorization": "Bearer exchanged-1"}


async def test_nothing_secret_reaches_the_log(idp, caplog):
    caplog.set_level(logging.DEBUG)
    p = _policy()
    await settle(p.materialise(_user()))
    await settle(p.materialise(_user()))
    idp.status, idp.body = 400, {"error": "invalid_grant"}
    with pytest.raises(AuthError):
        await settle(p.materialise(_user(raw_token="other")))
    text = caplog.text
    for secret in (SUBJECT, "exchanged-1", "s3cret", "other"):
        assert secret not in text


# --- health ---------------------------------------------------------------


def test_identity_report_describes_the_exchange_without_secrets(monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "s3cret")
    report = identity_report(_policy())
    assert report["mode"] == "exchange"
    assert report["probe_scope"] == "deployment-credential"
    assert report["exchange"] == {
        "token_url": TOKEN_URL,
        "audience": ["crm-api"],
        "client_auth": "client_secret_basic",
        "client_secret": "set",
    }
    assert "s3cret" not in json.dumps(report)
    monkeypatch.delenv(SECRET_VAR)
    assert identity_report(_policy())["exchange"]["client_secret"] == "unset"


# --- through the backings ---------------------------------------------------


def _fake_client(monkeypatch, seen):
    from beherouter.backends import mcp as mcp_backend

    class FakeClient:
        def __init__(self, transport):
            seen.append(transport)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def call_tool(self, verb, args):
            class R:
                data: ClassVar[dict] = {"ok": True}

            return R()

    monkeypatch.setattr(mcp_backend, "Client", FakeClient)


async def test_the_http_executor_forwards_the_exchanged_token(idp, monkeypatch):
    from beherouter.backends import mcp as mcp_backend
    from beherouter.backends.backing import McpBacking

    seen = []
    _fake_client(monkeypatch, seen)
    backing = McpBacking(
        name="crm",
        transport="http",
        url="https://crm.test/mcp",
        env={"authorization": "Bearer deployment"},
    )
    ex = mcp_backend.ReconnectingMCPExecutor(mcp_backend.build_transport(backing), backing)
    await ex.run("ping", {}, identity=_policy().materialise(_user()))
    assert seen[-1].headers["authorization"] == "Bearer exchanged-1"


async def test_a_guard_refusal_costs_no_exchange(idp, monkeypatch):
    from beherouter.backends import mcp as mcp_backend
    from beherouter.backends.backing import McpBacking

    def guard(verb, args):
        raise UsageError("refused by guard")

    seen = []
    _fake_client(monkeypatch, seen)
    backing = McpBacking(name="crm", transport="http", url="https://crm.test/mcp", guard=guard)
    ex = mcp_backend.ReconnectingMCPExecutor(mcp_backend.build_transport(backing), backing)
    with pytest.raises(UsageError, match="guard"):
        await ex.run("ping", {}, identity=_policy().materialise(_user()))
    assert idp.requests == [] and seen == []


async def test_a_failed_exchange_never_reaches_the_backend(idp, monkeypatch):
    from beherouter.backends import mcp as mcp_backend
    from beherouter.backends.backing import McpBacking

    idp.status, idp.body = 400, {"error": "invalid_grant"}
    seen = []
    _fake_client(monkeypatch, seen)
    backing = McpBacking(
        name="crm",
        transport="http",
        url="https://crm.test/mcp",
        env={"authorization": "Bearer deployment"},
    )
    ex = mcp_backend.ReconnectingMCPExecutor(mcp_backend.build_transport(backing), backing)
    with pytest.raises(AuthError):
        await ex.run("ping", {}, identity=_policy().materialise(_user()))
    assert seen == []  # no call, and certainly none with the deployment bearer


async def test_the_bound_client_executor_refuses_a_pending_identity(idp):
    from beherouter.backends.mcp import MCPClientExecutor

    with pytest.raises(UsageError):
        await MCPClientExecutor(client=None).run(
            "ping", {}, identity=_policy().materialise(_user())
        )
    assert idp.requests == []


async def test_the_inproc_openapi_backing_forwards_the_exchanged_token(
    idp, tmp_path, monkeypatch
):
    import beherouter.plugins.openapi as plugin
    from beherouter.gateway import load_backend

    doc = {
        "openapi": "3.0.0",
        "info": {"title": "crm", "version": "1"},
        "paths": {
            "/whoami": {
                "get": {
                    "operationId": "whoami",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    spec = tmp_path / "crm.json"
    spec.write_text(json.dumps(doc))
    upstream: list[httpx.Request] = []

    def handler(req):
        upstream.append(req)
        return httpx.Response(200, json={"auth": req.headers.get("authorization")})

    real = plugin.identity_client
    monkeypatch.setattr(
        plugin, "identity_client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    entry = RegistryEntry(
        name="crm",
        plugin="openapi",
        config={"spec": str(spec), "base_url": "https://crm.internal", "include": ["whoami"]},
        probe="whoami",
        pinned=["whoami"],
        env={"api_key": "${BEHEROUTER_CRM_API_KEY}"},
        identity=_table(),
    )
    from beherouter.plugins import get

    monkeypatch.setenv("BEHEROUTER_CRM_API_KEY", "deployment")
    validate_entry(entry)
    backend = await load_backend(entry)
    policy = policy_from_entry(entry, get("openapi").spec)
    out = await backend.executor.run("whoami", {}, identity=policy.materialise(_user()))
    assert out["result"]["auth"] == "Bearer exchanged-1"
    assert len(idp.requests) == 1


async def test_through_the_surface_path(idp, monkeypatch):
    """The surface's one dispatch path, with a policy whose request is fixed
    (no live FastMCP auth context), down to a real reconnecting executor."""
    from fastmcp import Client

    from beherouter.backends import mcp as mcp_backend
    from beherouter.backends.backing import McpBacking
    from beherouter.models import Backend, ToolDescriptor
    from beherouter.surface import build_surface

    seen = []
    _fake_client(monkeypatch, seen)
    backing = McpBacking(name="crm", transport="http", url="https://crm.test/mcp")
    ex = mcp_backend.ReconnectingMCPExecutor(mcp_backend.build_transport(backing), backing)

    base = _policy()

    class Fixed(type(base)):
        def resolve(self):
            return self.authorise(_user())

        def guard(self):
            self.gate(_user())

    policy = Fixed(**{f: getattr(base, f) for f in base.__dataclass_fields__})
    backend = Backend(
        name="crm",
        kind="mcp",
        descriptors=[
            ToolDescriptor(
                name="ping", verb="ping", summary="ping", schema={},
                pinned=True, mutating=False,
            )
        ],
        executor=ex,
    )
    async with Client(build_surface(backend, policy=policy)) as c:
        await c.call_tool("ping", {})
        await c.call_tool("ping", {})
    assert [t.headers["authorization"] for t in seen] == ["Bearer exchanged-1"] * 2
    assert len(idp.requests) == 1


@pytest.mark.parametrize("refused", [False, True])
async def test_health_probe_as_user_exercises_the_exchange(idp, monkeypatch, refused):
    """`health --deep --bearer-file` settles the exchange before the probe, so
    an IdP refusing the caller reads as `refused` (an identity verdict) and the
    backend is never called — not as a backend `failed`."""
    if refused:
        idp.status, idp.body = 400, {"error": "invalid_grant"}
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    from beherouter.auth import GatewayJWTVerifier
    from beherouter.backends import mcp as mcp_backend
    from beherouter.backends.backing import McpBacking
    from beherouter.health import check_entry
    from beherouter.models import Backend, ToolDescriptor

    kp = RSAKeyPair.generate()
    verifier = GatewayJWTVerifier(
        public_key=kp.public_key, issuer="https://idp.test", audience="gw"
    )
    token = kp.create_token(subject="alice", issuer="https://idp.test", audience="gw")

    seen = []
    _fake_client(monkeypatch, seen)
    backing = McpBacking(name="plane", transport="http", url="https://plane.test/mcp")

    async def load(entry):
        return Backend(
            name=entry.name,
            kind="mcp",
            descriptors=[
                ToolDescriptor(
                    name="member", verb="member", summary="", schema={},
                    pinned=True, mutating=False,
                )
            ],
            executor=mcp_backend.ReconnectingMCPExecutor(
                mcp_backend.build_transport(backing), backing
            ),
        )

    entry = RegistryEntry(
        name="plane",
        plugin="plane-http",
        config={"base_url": "http://plane-mcp-bearer:8211/bearer/mcp"},
        env={"access_token": "${BEHEROUTER_PLANE_ACCESS_TOKEN}"},
        pinned=["member"],
        identity=_table(),
    )
    rec = await check_entry(entry, load=load, user_token=token, verifier=verifier)
    if refused:
        assert rec["user_probe"]["state"] == "refused"
        assert "invalid_grant" in rec["user_probe"]["error"]
        # The deployment probe still runs; nothing exchanged reached the backend.
        assert all(
            not (t.headers or {}).get("authorization", "").startswith("Bearer exchanged")
            for t in seen
        )
        return
    assert rec["user_probe"]["state"] == "ok"
    assert idp.form()["subject_token"] == [token]
    assert seen[-1].headers["authorization"] == "Bearer exchanged-1"
    assert rec["identity"]["exchange"]["client_secret"] == "set"

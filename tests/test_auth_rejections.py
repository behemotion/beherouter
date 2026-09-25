"""Why a token was refused — told to the operator (WARNING, /metrics) and to
the client (RFC 6750 `error_description`).

The first incident of a per-user rollout is a client forwarding a stale token.
FastMCP logs that at INFO and answers it with the same generic 401 as a forged
token, so neither the operator nor the client can tell "refresh" from "you are
not who you say". These pin the difference, against FastMCP's REAL verifier,
so a FastMCP upgrade that rewords its log lines fails here rather than
silently degrading every reason to "invalid".
"""

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

from beherouter import auth
from beherouter.auth import (
    CompositeVerifier,
    GatewayJWTVerifier,
    ObservedVerifier,
    RejectionMiddleware,
    SharedTokenVerifier,
)

ISSUER = "https://idp.test"


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def info(self, msg, *a, **k):
        self.calls.append(("info", msg % a if a else msg))

    def warning(self, msg, *a, **k):
        self.calls.append(("warning", msg % a if a else msg))

    def debug(self, msg, *a, **k):
        self.calls.append(("debug", msg % a if a else msg))


@pytest.fixture
def pair():
    kp = RSAKeyPair.generate()
    v = GatewayJWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience="beherouter")
    rec = _Recorder()
    v.logger._inner = rec
    return kp, v, rec


@pytest.fixture(autouse=True)
def _fresh_counter():
    auth.REJECTIONS.clear()
    yield
    auth.REJECTIONS.clear()


def _expired(kp, **kw):
    return kp.create_token(
        subject="alice", issuer=ISSUER, audience="beherouter", expires_in_seconds=-60, **kw
    )


async def test_an_expired_signed_token_is_expired_and_warned(pair):
    kp, v, rec = pair
    observed = ObservedVerifier(v)
    assert await observed.verify_token(_expired(kp)) is None
    assert auth.REJECTIONS["expired"] == 1
    assert ("warning", "Bearer token rejected for client alice: token expired") in rec.calls
    assert not [c for c in rec.calls if c[0] == "info"]


async def test_a_forged_token_with_an_old_exp_is_invalid_not_expired(pair):
    """FastMCP checks the signature BEFORE exp, so "expired" can only ever be
    said about a token the IdP really signed."""
    _kp, v, _rec = pair
    forged = _expired(RSAKeyPair.generate())
    assert await ObservedVerifier(v).verify_token(forged) is None
    assert auth.REJECTIONS == {"invalid": 1}


async def test_a_wrong_audience_is_counted_as_audience(pair):
    kp, v, _rec = pair
    token = kp.create_token(subject="alice", issuer=ISSUER, audience="someone-else")
    assert await ObservedVerifier(v).verify_token(token) is None
    assert auth.REJECTIONS == {"audience": 1}


async def test_both_mode_counts_a_token_once(pair):
    kp, v, _rec = pair
    observed = ObservedVerifier(CompositeVerifier([SharedTokenVerifier("s3cret"), v]))
    assert await observed.verify_token(_expired(kp)) is None
    assert await observed.verify_token("not-the-shared-token") is None
    assert auth.REJECTIONS == {"expired": 1, "invalid": 1}


async def test_an_accepted_token_counts_nothing(pair):
    kp, v, _rec = pair
    token = kp.create_token(subject="alice", issuer=ISSUER, audience="beherouter")
    assert await ObservedVerifier(v).verify_token(token) is not None
    assert not auth.REJECTIONS


def test_metrics_emit_every_reason_zero_included():
    auth.REJECTIONS["expired"] += 2
    text = auth.metrics_text()
    assert 'beherouter_auth_rejections_total{reason="expired"} 2' in text
    for reason in auth.REASONS:
        assert f'reason="{reason}"' in text
    assert "# TYPE beherouter_auth_rejections_total counter" in text


def _app(verifier) -> Starlette:
    surface = FastMCP("demo", auth=ObservedVerifier(verifier))

    @surface.tool
    def ping() -> str:
        return "pong"

    sub = surface.http_app(path="/mcp")
    return Starlette(
        routes=[Mount("/demo", app=sub)],
        middleware=[Middleware(RejectionMiddleware)],
        lifespan=lambda app: sub.router.lifespan_context(sub),
    )


async def _post(app, token):
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        return await c.post(
            "/demo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )


async def test_an_expired_token_401_says_so_per_rfc6750(pair):
    kp, v, _rec = pair
    r = await _post(_app(v), _expired(kp))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == (
        'Bearer error="invalid_token", error_description="token expired"'
    )
    assert r.json() == {"error": "invalid_token", "error_description": "token expired"}


async def test_any_other_refusal_keeps_the_generic_answer(pair):
    _kp, v, _rec = pair
    r = await _post(_app(v), "garbage")
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert "token expired" not in r.headers["www-authenticate"]


async def test_a_valid_token_is_untouched(pair):
    kp, v, _rec = pair
    token = kp.create_token(subject="alice", issuer=ISSUER, audience="beherouter")
    r = await _post(_app(v), token)
    # Past auth: whatever MCP says about a session-less tools/list, it is
    # not a 401 and carries no challenge.
    assert r.status_code != 401
    assert "www-authenticate" not in r.headers


async def test_the_gateway_serves_metrics_unauthenticated(monkeypatch):
    from beherouter.gateway import build_gateway_app

    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    app = await build_gateway_app({})
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "beherouter_auth_rejections_total" in r.text


def test_the_gateway_audience_may_be_a_list(monkeypatch):
    """Several teams' surfaces, each with an audience of its own; the gateway
    accepts any of them and each surface narrows to its own."""
    monkeypatch.setenv("BEHEROUTER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("BEHEROUTER_OIDC_AUDIENCE", "plane-mcp, wiki-mcp")
    monkeypatch.setenv("BEHEROUTER_OIDC_JWKS_URI", "https://idp.test/jwks")
    v = auth.oidc_verifier()
    assert v.audience == ["plane-mcp", "wiki-mcp"]
    monkeypatch.setenv("BEHEROUTER_OIDC_AUDIENCE", "beherouter")
    assert auth.oidc_verifier().audience == "beherouter"


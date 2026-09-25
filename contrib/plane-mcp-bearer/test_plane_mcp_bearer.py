"""plane-mcp-bearer, end to end in-process: a real upstream tool call reaches a
fake Plane with the credential the wrapper was handed, on the header it belongs on.

Needs plane-mcp-server installed, so it is NOT part of the gateway's suite
(`testpaths = ["tests"]`). Run it in the wrapper's own environment:

    cd contrib/plane-mcp-bearer
    uv run --no-project --with plane-mcp-server==0.3.2 --with pytest \
        --with pytest-asyncio pytest -q
"""

import json

import httpx
import plane_mcp_bearer as pmb
import pytest
import requests
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

PAT = "plane_api_" + "0" * 32
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.sig"  # opaque to the wrapper

def _plane(request: httpx.Request) -> httpx.Response:
    """Plane's /users/me/: a PAT via X-Api-Key, a bearer via Authorization."""
    if request.headers.get("x-api-key") == PAT:
        return httpx.Response(200, json={"id": "svc", "email": "svc@x.invalid"})
    if request.headers.get("authorization") == f"Bearer {JWT}":
        return httpx.Response(200, json={"id": "alice", "email": "alice@x.invalid"})
    return httpx.Response(401, json={"detail": "nope"})


def _verifier(**kw) -> pmb.ForwardingVerifier:
    return pmb.ForwardingVerifier(
        workspace_slug="acme", transport=httpx.MockTransport(_plane), **kw
    )


async def test_a_pat_is_labelled_for_upstreams_api_key_route():
    tok = await _verifier().verify_token(PAT)
    assert tok.claims["auth_method"] == "api_key_header"
    assert tok.claims["workspace_slug"] == "acme"
    assert tok.claims["sub"] == "svc"


async def test_anything_else_is_labelled_outside_upstreams_api_key_pair():
    tok = await _verifier().verify_token(JWT)
    assert tok.claims["auth_method"] not in ("api_key_env", "api_key_header")
    assert tok.claims["sub"] == "alice"


async def test_a_token_plane_refuses_is_refused():
    assert await _verifier().verify_token("garbage") is None


async def test_the_verdict_is_cached_per_token():
    calls = []

    def counting(request):
        calls.append(1)
        return _plane(request)

    v = pmb.ForwardingVerifier(workspace_slug="acme", transport=httpx.MockTransport(counting))
    await v.verify_token(JWT)
    await v.verify_token(JWT)
    assert len(calls) == 1


def test_an_unpinned_upstream_refuses_to_start(monkeypatch):
    monkeypatch.delenv("PLANE_MCP_BEARER_ALLOW_UNPINNED", raising=False)
    with pytest.raises(SystemExit, match="re-verify"):
        pmb.check_upstream("9.9.9")
    pmb.check_upstream(pmb.UPSTREAM_VERSION)


def test_the_installed_upstream_is_the_pinned_one():
    pmb.check_upstream()


@pytest.mark.parametrize(
    ("token", "expect"),
    [(PAT, ("x-api-key", PAT)), (JWT, ("authorization", f"Bearer {JWT}"))],
)
async def test_a_real_tool_call_reaches_plane_on_the_right_header(monkeypatch, token, expect):
    """Through upstream's own `member` tool and the Plane SDK — the private
    routing this wrapper depends on, exercised rather than asserted."""
    monkeypatch.setenv("PLANE_BASE_URL", "http://plane.invalid")
    seen = []

    def fake_request(self, method, url, **kw):  # the Plane SDK uses requests
        seen.append({k.lower(): v for k, v in (kw.get("headers") or {}).items()})
        r = requests.Response()
        r.status_code = 200
        r._content = json.dumps(
            {"id": "u", "email": "u@x.invalid", "display_name": "u",
             "first_name": "u", "last_name": "u"}
        ).encode()
        r.headers["Content-Type"] = "application/json"
        return r

    monkeypatch.setattr(requests.Session, "request", fake_request)
    app = pmb.build_app(verifier=_verifier(), path="/bearer")
    transport = StreamableHttpTransport(
        "http://wrapper.invalid/bearer/mcp",
        headers={"authorization": f"Bearer {token}"},
        httpx_client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), **kw
        ),
    )
    async with app.router.lifespan_context(app), Client(transport) as client:
        await client.call_tool("member", {"action": "me"})
    header, value = expect
    assert seen, "the Plane SDK was never called"
    assert seen[-1].get(header) == value
    other = "authorization" if header == "x-api-key" else "x-api-key"
    assert other not in seen[-1]

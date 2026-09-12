import httpx
import pytest

from beherouter.errors import AuthError, Unavailable
from beherouter.plugins.calendar.oauth import RefreshTokenAuth


def _auth(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("token_url", "https://oauth.example/token")
    kw.setdefault("client_id", "cid")
    kw.setdefault("refresh_token", "rt")
    return RefreshTokenAuth(client=client, **kw)


async def test_fetches_an_access_token():
    def handler(request):
        return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})

    assert await _auth(handler).access_token() == "at-1"


async def test_posts_the_refresh_grant_as_form_data():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["ct"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    auth = _auth(handler, client_secret="sec", scope="offline_access Calendars.ReadWrite")
    await auth.access_token()
    assert "grant_type=refresh_token" in seen["body"]
    assert "refresh_token=rt" in seen["body"]
    assert "client_id=cid" in seen["body"]
    assert "client_secret=sec" in seen["body"]
    assert "scope=" in seen["body"]
    assert "application/x-www-form-urlencoded" in seen["ct"]


async def test_omits_client_secret_when_absent():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    await _auth(handler).access_token()
    assert "client_secret" not in seen["body"]


async def test_caches_until_expiry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    now = [1000.0]
    auth = _auth(handler, clock=lambda: now[0])
    await auth.access_token()
    now[0] += 100
    await auth.access_token()
    assert len(calls) == 1


async def test_refetches_after_expiry():
    tokens = iter(["at-1", "at-2"])

    def handler(request):
        return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})

    now = [1000.0]
    auth = _auth(handler, clock=lambda: now[0])
    assert await auth.access_token() == "at-1"
    now[0] += 3600
    assert await auth.access_token() == "at-2"


async def test_refetches_inside_the_skew_window():
    """A token expiring in 30s must be replaced, not handed out."""
    tokens = iter(["at-1", "at-2"])

    def handler(request):
        return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})

    now = [1000.0]
    auth = _auth(handler, clock=lambda: now[0])
    await auth.access_token()
    now[0] += 3600 - 30
    assert await auth.access_token() == "at-2"


async def test_invalid_grant_is_an_auth_error():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(AuthError, match="invalid_grant"):
        await _auth(handler).access_token()


async def test_auth_error_never_leaks_the_refresh_token():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    auth = _auth(handler, refresh_token="SUPERSECRET")
    with pytest.raises(AuthError) as ei:
        await auth.access_token()
    assert "SUPERSECRET" not in str(ei.value)


async def test_server_error_is_unavailable():
    def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(Unavailable):
        await _auth(handler).access_token()


async def test_transport_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(Unavailable):
        await _auth(handler).access_token()


async def test_response_without_an_access_token_is_unavailable():
    def handler(request):
        return httpx.Response(200, json={"token_type": "Bearer"})

    with pytest.raises(Unavailable, match="no access_token"):
        await _auth(handler).access_token()


# --- the leak-prone branch ---------------------------------------------------


async def test_transport_error_never_leaks_the_refresh_token():
    """⚠️ THE branch that actually holds the secret: an httpx error carries its
    request, and the refresh grant's request body IS the refresh token. The
    400-response branch never had the token in scope; this one does."""

    def handler(request):
        raise httpx.ConnectError(
            "boom",
            request=httpx.Request(
                "POST", "https://oauth.example/token", data={"refresh_token": "SUPERSECRET"}
            ),
        )

    auth = _auth(handler, refresh_token="SUPERSECRET")
    with pytest.raises(Unavailable) as ei:
        await auth.access_token()
    assert "SUPERSECRET" not in str(ei.value)
    assert "SUPERSECRET" not in repr(ei.value)


# --- refresh token rotation --------------------------------------------------


async def test_a_rotated_refresh_token_is_used_on_the_next_refresh():
    """⚠️ Microsoft returns a REPLACEMENT refresh token on every refresh and
    invalidates the old one. Discarding it makes m365 work for about an hour and
    then fail with the exact symptom of the `common`-authority trap."""
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": "at", "expires_in": 3600, "refresh_token": f"rt-{len(sent)}"},
        )

    now = [1000.0]
    auth = _auth(handler, clock=lambda: now[0])
    await auth.access_token()
    now[0] += 3600
    await auth.access_token()
    assert "refresh_token=rt&" in sent[0] + "&"
    assert "refresh_token=rt-1" in sent[1]


async def test_a_response_without_a_rotation_keeps_the_original_token():
    """Google does not rotate; an absent field must not blank the stored one."""
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    now = [1000.0]
    auth = _auth(handler, clock=lambda: now[0])
    await auth.access_token()
    now[0] += 3600
    await auth.access_token()
    assert all("refresh_token=rt&" in body + "&" for body in sent)


# --- an untrustworthy payload ------------------------------------------------


async def test_a_non_numeric_expires_in_does_not_raise():
    """float("soon") is a ValueError, which is not an AxiError and would
    traceback `health --deep` instead of failing one backend."""

    def handler(request):
        return httpx.Response(200, json={"access_token": "at", "expires_in": "soon"})

    assert await _auth(handler).access_token() == "at"


async def test_a_null_expires_in_does_not_raise():
    def handler(request):
        return httpx.Response(200, json={"access_token": "at", "expires_in": None})

    assert await _auth(handler).access_token() == "at"


async def test_a_json_array_body_is_unavailable():
    """payload.get(...) on a list is an AttributeError — not an AxiError."""

    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(Unavailable):
        await _auth(handler).access_token()

"""Routing a request to the full or the lean app (spec 2026-11-22)."""

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from beherouter.profiles import ProfileDispatcher


def _app(tag, issue=None, status=200):
    async def ep(request):
        headers = {"mcp-session-id": issue} if issue else {}
        return PlainTextResponse(tag, headers=headers, status_code=status)
    return Starlette(routes=[Route("/mcp", ep, methods=["GET", "POST", "DELETE"])])


async def _get(d, url, method="POST", sid=None):
    headers = {"mcp-session-id": sid} if sid else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d), base_url="http://t") as c:
        return await c.request(method, url, headers=headers)


async def test_no_profile_is_full():
    assert (await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp")).text == "F"


async def test_profile_lean_is_lean():
    assert (await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=lean")).text == "L"


async def test_profile_full_is_full():
    assert (await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=full")).text == "F"


async def test_unknown_profile_is_400_without_echo():
    r = await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=<b>tiny</b>")
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "tiny" not in r.text


async def test_empty_profile_is_400():
    r = await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=")
    assert r.status_code == 400


async def test_uppercase_profile_is_400():
    r = await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=LEAN")
    assert r.status_code == 400


async def test_repeated_profile_last_wins():
    d = ProfileDispatcher(_app("F"), _app("L"))
    assert (await _get(d, "/mcp?profile=full&profile=lean")).text == "L"
    assert (await _get(d, "/mcp?profile=lean&profile=full")).text == "F"


async def test_url_encoded_profile_is_decoded():
    assert (await _get(ProfileDispatcher(_app("F"), _app("L")), "/mcp?profile=le%61n")).text == "L"


async def test_session_id_wins_over_query():
    d = ProfileDispatcher(_app("F"), _app("L", issue="s1"))
    await _get(d, "/mcp?profile=lean")
    assert d.sessions == {"s1": "lean"}
    assert (await _get(d, "/mcp", sid="s1")).text == "L"


async def test_session_id_wins_over_explicit_full_query():
    d = ProfileDispatcher(_app("F"), _app("L", issue="s1"))
    await _get(d, "/mcp?profile=lean")
    assert (await _get(d, "/mcp?profile=full", sid="s1")).text == "L"


async def test_unknown_session_routes_by_query():
    d = ProfileDispatcher(_app("F"), _app("L"))
    assert (await _get(d, "/mcp?profile=lean", sid="gone")).text == "L"


async def test_delete_forgets_the_session():
    d = ProfileDispatcher(_app("F"), _app("L", issue="s1"))
    await _get(d, "/mcp?profile=lean")
    r = await _get(d, "/mcp", method="DELETE", sid="s1")
    assert r.text == "L" and d.sessions == {}


async def test_a_refused_delete_keeps_the_session():
    for status in (404, 405):
        d = ProfileDispatcher(_app("F"), _app("L", issue="s1"))
        await _get(d, "/mcp?profile=lean")
        d.apps["lean"] = _app("L", status=status)
        await _get(d, "/mcp", method="DELETE", sid="s1")
        assert d.sessions == {"s1": "lean"}


async def test_session_map_is_bounded():
    d = ProfileDispatcher(_app("F", issue="x"), _app("L"), max_sessions=2)
    for sid in ("a", "b", "c"):
        d._remember(sid, "full")
    assert list(d.sessions) == ["b", "c"]


async def test_a_used_session_survives_eviction():
    d = ProfileDispatcher(_app("F"), _app("L"), max_sessions=2)
    d._remember("a", "lean")
    d._remember("b", "lean")
    await _get(d, "/mcp", sid="a")  # touches a: b is now the oldest
    d._remember("c", "lean")
    assert list(d.sessions) == ["a", "c"]


async def test_non_http_scope_goes_to_full():
    seen = []

    async def full(scope, receive, send):
        seen.append(scope["type"])

    async def lean(scope, receive, send):  # pragma: no cover
        raise AssertionError

    await ProfileDispatcher(full, lean)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]

"""Routing a request to the full or the lean app (spec 2026-11-22)."""

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from beherouter.profiles import ProfileDispatcher


def _app(tag, issue=None):
    async def ep(request):
        headers = {"mcp-session-id": issue} if issue else {}
        return PlainTextResponse(tag, headers=headers)
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


async def test_session_map_is_bounded():
    d = ProfileDispatcher(_app("F", issue="x"), _app("L"), max_sessions=2)
    for sid in ("a", "b", "c"):
        d._remember(sid, "full")
    assert list(d.sessions) == ["b", "c"]


async def test_non_http_scope_goes_to_full():
    seen = []

    async def full(scope, receive, send):
        seen.append(scope["type"])

    async def lean(scope, receive, send):  # pragma: no cover
        raise AssertionError

    await ProfileDispatcher(full, lean)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]

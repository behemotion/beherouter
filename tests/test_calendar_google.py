import json

import httpx
import pytest

from beherouter.errors import AuthError, NotFound, Unavailable, UsageError
from beherouter.plugins.calendar.oauth import RefreshTokenAuth
from beherouter.plugins.calendar.providers.google import GoogleCalendar


def _provider(handler, **kw):
    def routed(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(routed))
    auth = RefreshTokenAuth(
        token_url="https://oauth.example/token",
        client_id="cid",
        refresh_token="rt",
        client=client,
    )
    return GoogleCalendar(auth=auth, client=client, **kw)


async def test_list_calendars_normalises_the_payload():
    def handler(request):
        assert request.url.path == "/calendar/v3/users/me/calendarList"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "primary@example.com", "summary": "Work", "primary": True},
                    {"id": "team@example.com", "summary": "Team"},
                ]
            },
        )

    out = await _provider(handler).list_calendars()
    assert out == {
        "calendars": [
            {"id": "primary@example.com", "name": "Work", "primary": True},
            {"id": "team@example.com", "name": "Team", "primary": False},
        ]
    }


async def test_list_calendars_sends_the_bearer_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": []})

    await _provider(handler).list_calendars()
    assert seen["auth"] == "Bearer at"


async def test_list_events_expands_recurrences_and_orders_by_start():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert seen["params"]["singleEvents"] == "true"
    assert seen["params"]["orderBy"] == "startTime"
    assert seen["params"]["timeMin"] == "2026-09-15T00:00:00+06:00"
    assert seen["params"]["timeMax"] == "2026-09-16T00:00:00+06:00"


async def test_list_events_normalises_an_event():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ev1",
                        "summary": "Standup",
                        "location": "Room 2",
                        "start": {"dateTime": "2026-09-15T09:00:00+06:00"},
                        "end": {"dateTime": "2026-09-15T09:15:00+06:00"},
                        "organizer": {"email": "boss@example.com"},
                        "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}],
                    }
                ]
            },
        )

    out = await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert out["events"] == [
        {
            "id": "ev1",
            "summary": "Standup",
            "start": "2026-09-15T09:00:00+06:00",
            "end": "2026-09-15T09:15:00+06:00",
            "location": "Room 2",
            "attendees": ["a@example.com", "b@example.com"],
            "organizer": "boss@example.com",
        }
    ]


async def test_list_events_handles_an_all_day_event():
    """All-day events carry `date`, not `dateTime`. Dropping them loses real busy time."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ev2",
                        "summary": "Holiday",
                        "start": {"date": "2026-09-15"},
                        "end": {"date": "2026-09-16"},
                    }
                ]
            },
        )

    out = await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert out["events"][0]["start"] == "2026-09-15"


async def test_list_events_rejects_a_backwards_window():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="end must be after start"):
        await _provider(handler).list_events(
            start="2026-09-16T00:00:00+06:00", end="2026-09-15T00:00:00+06:00"
        )


async def test_get_freebusy_posts_the_calendar_ids():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"calendars": {}})

    await _provider(handler).get_freebusy(
        start="2026-09-15T00:00:00+06:00",
        end="2026-09-16T00:00:00+06:00",
        calendar_ids=["a@example.com", "b@example.com"],
    )
    assert seen["body"]["items"] == [{"id": "a@example.com"}, {"id": "b@example.com"}]


async def test_get_freebusy_flattens_busy_intervals_per_calendar():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "a@example.com": {
                        "busy": [
                            {"start": "2026-09-15T09:00:00Z", "end": "2026-09-15T10:00:00Z"}
                        ]
                    },
                    "b@example.com": {"busy": []},
                }
            },
        )

    out = await _provider(handler).get_freebusy(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert out["busy"] == [
        {
            "calendar_id": "a@example.com",
            "start": "2026-09-15T09:00:00Z",
            "end": "2026-09-15T10:00:00Z",
        }
    ]


async def test_get_freebusy_surfaces_a_per_calendar_error():
    """Google reports a bad calendar id INSIDE a 200. Silently returning "free"
    would let an agent book straight over a real meeting."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "bad@example.com": {
                        "errors": [{"domain": "global", "reason": "notFound"}],
                        "busy": [],
                    }
                }
            },
        )

    with pytest.raises(Unavailable, match="notFound"):
        await _provider(handler).get_freebusy(
            start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
        )


async def test_401_is_an_auth_error():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    with pytest.raises(AuthError):
        await _provider(handler).list_calendars()


async def test_404_is_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    with pytest.raises(NotFound):
        await _provider(handler).list_calendars()


async def test_400_is_a_usage_error():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "Bad Request"}})

    with pytest.raises(UsageError):
        await _provider(handler).list_calendars()


async def test_500_is_unavailable():
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(Unavailable):
        await _provider(handler).list_calendars()


async def test_create_event_builds_the_google_body():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return httpx.Response(200, json={"id": "new1", "summary": "Sync"})

    await _provider(handler).create_event(
        summary="Sync",
        start="2026-09-15T14:00:00+06:00",
        end="2026-09-15T15:00:00+06:00",
        attendees=["a@example.com"],
        location="Room 2",
        description="agenda",
    )
    assert seen["method"] == "POST"
    assert seen["body"]["summary"] == "Sync"
    assert seen["body"]["start"] == {"dateTime": "2026-09-15T14:00:00+06:00"}
    assert seen["body"]["end"] == {"dateTime": "2026-09-15T15:00:00+06:00"}
    assert seen["body"]["attendees"] == [{"email": "a@example.com"}]
    assert seen["body"]["location"] == "Room 2"
    assert seen["body"]["description"] == "agenda"


async def test_create_event_asks_google_to_send_invitations():
    """Without sendUpdates the attendees are recorded and never told."""
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"id": "new1"})

    await _provider(handler).create_event(
        summary="Sync",
        start="2026-09-15T14:00:00+06:00",
        end="2026-09-15T15:00:00+06:00",
        attendees=["a@example.com"],
    )
    assert seen["params"]["sendUpdates"] == "all"


async def test_create_event_returns_the_normalised_event():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "new1",
                "summary": "Sync",
                "start": {"dateTime": "2026-09-15T14:00:00+06:00"},
                "end": {"dateTime": "2026-09-15T15:00:00+06:00"},
            },
        )

    out = await _provider(handler).create_event(
        summary="Sync", start="2026-09-15T14:00:00+06:00", end="2026-09-15T15:00:00+06:00"
    )
    assert out["event"]["id"] == "new1"
    assert out["event"]["start"] == "2026-09-15T14:00:00+06:00"


async def test_create_event_rejects_a_naive_start():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="must include a UTC offset"):
        await _provider(handler).create_event(
            summary="Sync", start="2026-09-15T14:00:00", end="2026-09-15T15:00:00+06:00"
        )


async def test_update_event_patches_only_the_supplied_fields():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev1"})

    await _provider(handler).update_event(event_id="ev1", summary="Renamed")
    assert seen["method"] == "PATCH"
    assert seen["body"] == {"summary": "Renamed"}


async def test_update_event_can_move_an_event():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev1"})

    await _provider(handler).update_event(
        event_id="ev1", start="2026-09-16T14:00:00+06:00", end="2026-09-16T15:00:00+06:00"
    )
    assert seen["body"]["start"] == {"dateTime": "2026-09-16T14:00:00+06:00"}
    assert "summary" not in seen["body"]


async def test_update_event_with_no_changes_is_a_usage_error():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="no fields to update"):
        await _provider(handler).update_event(event_id="ev1")


async def test_update_event_validates_a_window_when_both_ends_are_given():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="end must be after start"):
        await _provider(handler).update_event(
            event_id="ev1", start="2026-09-16T15:00:00+06:00", end="2026-09-16T14:00:00+06:00"
        )


async def test_update_event_rejects_a_naive_start_on_its_own():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="must include a UTC offset"):
        await _provider(handler).update_event(event_id="ev1", start="2026-09-16T15:00:00")


async def test_delete_event_reports_success():
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(204)

    out = await _provider(handler).delete_event(event_id="ev1")
    assert out == {"deleted": True, "event_id": "ev1"}


async def test_delete_event_on_a_missing_id_is_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    with pytest.raises(NotFound):
        await _provider(handler).delete_event(event_id="gone")


# --- path safety -------------------------------------------------------------


async def test_a_calendar_id_with_a_hash_is_percent_encoded():
    """Google's own holiday calendars carry '#' and '@' and list_calendars hands
    those ids to the agent. Unquoted, the '#' opens a fragment and the path
    truncates to /calendars/en.usa — a different, existing-looking resource."""
    seen = {}

    def handler(request):
        seen["raw"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"items": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00",
        end="2026-09-16T00:00:00+06:00",
        calendar_id="en.usa#holiday@group.v.calendar.google.com",
    )
    assert seen["raw"].startswith(
        "/calendar/v3/calendars/en.usa%23holiday%40group.v.calendar.google.com/events"
    )


async def test_a_traversal_segment_cannot_escape_its_position():
    """The grant is the BROAD calendar scope, so a '..' segment would reach the
    whole Calendar API under the same credential — dissolving the six-verb list
    that is this surface's security boundary."""
    seen = {}

    def handler(request):
        seen["raw"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"id": "ev1"})

    await _provider(handler).update_event(
        event_id="../../secret", calendar_id="x/../..", summary="Renamed"
    )
    assert seen["raw"].startswith(
        "/calendar/v3/calendars/x%2F..%2F../events/..%2F..%2Fsecret"
    )


async def test_a_transport_error_never_leaks_the_access_token():
    """An httpx error carries its request, and the request carries the bearer
    token. `{e}` here would print the credential into the gateway's logs."""

    def handler(request):
        raise httpx.ConnectError(
            "boom",
            request=httpx.Request(
                "GET",
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers={"Authorization": "Bearer SUPERSECRET"},
            ),
        )

    with pytest.raises(Unavailable) as ei:
        await _provider(handler).list_calendars()
    assert "SUPERSECRET" not in str(ei.value)

import json

import httpx
import pytest

from beherouter.errors import AuthError, Unavailable, UsageError
from beherouter.plugins.calendar.oauth import RefreshTokenAuth
from beherouter.plugins.calendar.providers.microsoft import TOKEN_URL, MicrosoftCalendar


def _provider(handler, **kw):
    def routed(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(routed))
    auth = RefreshTokenAuth(
        token_url=TOKEN_URL, client_id="cid", refresh_token="rt", client=client
    )
    return MicrosoftCalendar(auth=auth, client=client, **kw)


def test_token_url_uses_the_consumers_authority():
    """⚠️ `common` issues refresh tokens that are REJECTED AT THE FIRST REFRESH:
    everything works for about an hour and then the surface dies. This is an
    invariant, not a preference."""
    assert "/consumers/" in TOKEN_URL
    assert "/common/" not in TOKEN_URL


async def test_list_calendars_normalises_the_payload():
    def handler(request):
        assert request.url.path == "/v1.0/me/calendars"
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "AAA", "name": "Calendar", "isDefaultCalendar": True},
                    {"id": "BBB", "name": "Birthdays"},
                ]
            },
        )

    out = await _provider(handler).list_calendars()
    assert out == {
        "calendars": [
            {"id": "AAA", "name": "Calendar", "primary": True},
            {"id": "BBB", "name": "Birthdays", "primary": False},
        ]
    }


async def test_requests_pin_the_timezone_to_utc():
    """Graph returns a NAIVE dateTime plus a timeZone field; pinning it to UTC
    makes normalisation deterministic."""
    seen = {}

    def handler(request):
        seen["prefer"] = request.headers.get("prefer", "")
        return httpx.Response(200, json={"value": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert 'outlook.timezone="UTC"' in seen["prefer"]


async def test_list_events_uses_calendarview_with_the_window():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"value": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    # calendarView (not /events) is what expands a recurring series into
    # occurrences — /events returns the master carrying a recurrence rule.
    assert seen["path"] == "/v1.0/me/calendarView"
    assert seen["params"]["startDateTime"] == "2026-09-15T00:00:00+06:00"
    assert seen["params"]["endDateTime"] == "2026-09-16T00:00:00+06:00"


async def test_list_events_targets_a_named_calendar():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"value": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00",
        end="2026-09-16T00:00:00+06:00",
        calendar_id="AAA",
    )
    assert seen["path"] == "/v1.0/me/calendars/AAA/calendarView"


async def test_list_events_normalises_utc_datetimes_to_rfc3339():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "ev1",
                        "subject": "Standup",
                        "start": {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-09-15T09:15:00.0000000", "timeZone": "UTC"},
                        "location": {"displayName": "Room 2"},
                        "organizer": {"emailAddress": {"address": "boss@example.com"}},
                        "attendees": [
                            {"emailAddress": {"address": "a@example.com"}},
                            {"emailAddress": {"address": "b@example.com"}},
                        ],
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
            "start": "2026-09-15T09:00:00Z",
            "end": "2026-09-15T09:15:00Z",
            "location": "Room 2",
            "attendees": ["a@example.com", "b@example.com"],
            "organizer": "boss@example.com",
        }
    ]


async def test_list_events_rejects_a_backwards_window():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="end must be after start"):
        await _provider(handler).list_events(
            start="2026-09-16T00:00:00+06:00", end="2026-09-15T00:00:00+06:00"
        )


async def test_get_freebusy_derives_busy_from_events():
    """getSchedule is a work/school feature; this surface is a personal account."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "ev1",
                        "subject": "Standup",
                        "showAs": "busy",
                        "start": {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-09-15T09:15:00.0000000", "timeZone": "UTC"},
                    }
                ]
            },
        )

    out = await _provider(handler).get_freebusy(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert out["busy"] == [
        {
            "calendar_id": "default",
            "start": "2026-09-15T09:00:00Z",
            "end": "2026-09-15T09:15:00Z",
        }
    ]


async def test_get_freebusy_ignores_events_marked_free():
    """An event marked free does NOT block the slot; busy and oof do."""

    def handler(request):
        def ev(eid, show_as):
            return {
                "id": eid,
                "subject": eid,
                "showAs": show_as,
                "start": {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-15T09:15:00.0000000", "timeZone": "UTC"},
            }

        return httpx.Response(
            200, json={"value": [ev("a", "free"), ev("b", "busy"), ev("c", "oof")]}
        )

    out = await _provider(handler).get_freebusy(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00"
    )
    assert len(out["busy"]) == 2


async def test_get_freebusy_spans_multiple_calendars():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"value": []})

    out = await _provider(handler).get_freebusy(
        start="2026-09-15T00:00:00+06:00",
        end="2026-09-16T00:00:00+06:00",
        calendar_ids=["AAA", "BBB"],
    )
    assert calls == [
        "/v1.0/me/calendars/AAA/calendarView",
        "/v1.0/me/calendars/BBB/calendarView",
    ]
    assert out["busy"] == []


async def test_401_is_an_auth_error():
    def handler(request):
        return httpx.Response(
            401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}
        )

    with pytest.raises(AuthError):
        await _provider(handler).list_calendars()


async def test_create_event_builds_the_graph_body():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "new1", "subject": "Sync"})

    await _provider(handler).create_event(
        summary="Sync",
        start="2026-09-15T14:00:00+06:00",
        end="2026-09-15T15:00:00+06:00",
        attendees=["a@example.com"],
        location="Room 2",
        description="agenda",
    )
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1.0/me/events"
    assert seen["body"]["subject"] == "Sync"
    # Converted to UTC and offset-free: Graph reads `dateTime` in `timeZone`,
    # and 14:00+06:00 is 08:00Z.
    assert seen["body"]["start"] == {"dateTime": "2026-09-15T08:00:00", "timeZone": "UTC"}
    assert seen["body"]["end"] == {"dateTime": "2026-09-15T09:00:00", "timeZone": "UTC"}
    assert seen["body"]["location"] == {"displayName": "Room 2"}
    assert seen["body"]["body"] == {"contentType": "text", "content": "agenda"}
    assert seen["body"]["attendees"] == [
        {"emailAddress": {"address": "a@example.com"}, "type": "required"}
    ]


async def test_create_event_targets_a_named_calendar():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(201, json={"id": "new1"})

    await _provider(handler).create_event(
        summary="Sync",
        start="2026-09-15T14:00:00+06:00",
        end="2026-09-15T15:00:00+06:00",
        calendar_id="AAA",
    )
    assert seen["path"] == "/v1.0/me/calendars/AAA/events"


async def test_create_event_returns_the_normalised_event():
    def handler(request):
        return httpx.Response(
            201,
            json={
                "id": "new1",
                "subject": "Sync",
                "start": {"dateTime": "2026-09-15T08:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"},
            },
        )

    out = await _provider(handler).create_event(
        summary="Sync", start="2026-09-15T14:00:00+06:00", end="2026-09-15T15:00:00+06:00"
    )
    assert out["event"] == {
        "id": "new1",
        "summary": "Sync",
        "start": "2026-09-15T08:00:00Z",
        "end": "2026-09-15T09:00:00Z",
        "location": "",
        "attendees": [],
        "organizer": "",
    }


async def test_create_event_rejects_a_naive_start():
    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(UsageError, match="must include a UTC offset"):
        await _provider(handler).create_event(
            summary="Sync", start="2026-09-15T14:00:00", end="2026-09-15T15:00:00+06:00"
        )


async def test_update_event_patches_by_global_event_id():
    """Graph event ids are global: /me/events/{id} needs no calendar segment."""
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev1"})

    await _provider(handler, calendar_id="AAA").update_event(event_id="ev1", summary="Renamed")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/v1.0/me/events/ev1"
    assert seen["body"] == {"subject": "Renamed"}


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


async def test_delete_event_reports_success():
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/v1.0/me/events/ev1"
        return httpx.Response(204)

    out = await _provider(handler).delete_event(event_id="ev1")
    assert out == {"deleted": True, "event_id": "ev1"}


# --- path safety -------------------------------------------------------------


async def test_a_calendar_id_with_a_hash_is_percent_encoded():
    """A '#' in a calendar id opens a fragment: unquoted, the request path
    truncates and lands on a different resource than the agent asked for."""
    seen = {}

    def handler(request):
        seen["raw"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"value": []})

    await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00",
        end="2026-09-16T00:00:00+06:00",
        calendar_id="AA#BB@example.com",
    )
    assert seen["raw"].startswith("/v1.0/me/calendars/AA%23BB%40example.com/calendarView")


async def test_a_traversal_segment_cannot_escape_its_position():
    """A '..' segment would reach the rest of Graph under the same
    Calendars.ReadWrite grant, past the six verbs this surface advertises."""
    seen = {}

    def handler(request):
        seen["raw"] = request.url.raw_path.decode()
        return httpx.Response(204)

    await _provider(handler).delete_event(event_id="x/../../me/messages")
    assert seen["raw"] == "/v1.0/me/events/x%2F..%2F..%2Fme%2Fmessages"


async def test_a_non_utc_offset_is_sent_as_the_correct_utc_wall_clock():
    """⚠️ Graph interprets `dateTime` in `timeZone`, ignoring any offset inside
    the string. Sending 14:00+06:00 beside timeZone: "UTC" would book at 14:00Z
    — six hours late — which is precisely what times.py exists to prevent."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "new1"})

    await _provider(handler).create_event(
        summary="Sync", start="2026-09-15T14:00:00+06:00", end="2026-09-15T15:30:00+06:00"
    )
    assert seen["body"]["start"] == {"dateTime": "2026-09-15T08:00:00", "timeZone": "UTC"}
    assert seen["body"]["end"] == {"dateTime": "2026-09-15T09:30:00", "timeZone": "UTC"}


async def test_a_utc_input_is_sent_unchanged_and_offset_free():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev1"})

    await _provider(handler).update_event(
        event_id="ev1", start="2026-09-15T08:00:00Z", end="2026-09-15T09:00:00Z"
    )
    assert seen["body"]["start"] == {"dateTime": "2026-09-15T08:00:00", "timeZone": "UTC"}


async def test_a_transport_error_never_leaks_the_access_token():
    """An httpx error carries its request, and the request carries the bearer
    token. `{e}` here would print the credential into the gateway's logs."""

    def handler(request):
        raise httpx.ConnectError(
            "boom",
            request=httpx.Request(
                "GET",
                "https://graph.microsoft.com/v1.0/me/calendars",
                headers={"Authorization": "Bearer SUPERSECRET"},
            ),
        )

    with pytest.raises(Unavailable) as ei:
        await _provider(handler).list_calendars()
    assert "SUPERSECRET" not in str(ei.value)


# --- paging ------------------------------------------------------------------


async def test_get_freebusy_refuses_a_paged_window():
    """⚠️ A missed event here reads as 'free' and books over a real meeting,
    with no signal. Refusing is a retryable error; under-reporting is silent."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skip=250",
                "value": [
                    {
                        "id": "ev1",
                        "showAs": "busy",
                        "start": {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-09-15T09:15:00.0000000", "timeZone": "UTC"},
                    }
                ],
            },
        )

    with pytest.raises(Unavailable, match="narrow it"):
        await _provider(handler).get_freebusy(
            start="2026-09-15T00:00:00+06:00", end="2026-12-15T00:00:00+06:00"
        )


async def test_list_events_truncates_at_max_results_without_refusing():
    """The asymmetry is deliberate: max_results is the caller's own cap, and
    Google's maxResults truncates identically. Only free/busy, where silence
    means 'free', is worth refusing."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skip=1",
                "value": [{"id": "ev1", "subject": "Standup"}],
            },
        )

    out = await _provider(handler).list_events(
        start="2026-09-15T00:00:00+06:00", end="2026-09-16T00:00:00+06:00", max_results=1
    )
    assert [e["id"] for e in out["events"]] == ["ev1"]

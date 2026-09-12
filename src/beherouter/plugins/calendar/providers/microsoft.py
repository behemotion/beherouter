"""Microsoft Graph v1.0 adapter, targeting a PERSONAL Microsoft account.

⚠️ TOKEN_URL USES THE `consumers` AUTHORITY, NOT `common`. This is the single
most dangerous detail in the whole plugin: a refresh token issued via `common`
is REJECTED AT THE FIRST REFRESH. Everything works for about an hour and then
the surface dies, which reads as anything but an authority mismatch.
`test_token_url_uses_the_consumers_authority` holds this as an invariant.

⚠️ get_freebusy derives from calendarView rather than from Graph's
`getSchedule`. getSchedule is documented for work/school accounts; on a personal
account it is not dependable. Deriving busy windows from the events themselves
is one call, always works, and returns the same normalised shape.
"""

from datetime import UTC
from urllib.parse import quote

import httpx

from ....errors import Unavailable, UsageError
from ..oauth import RefreshTokenAuth
from ..times import require_offset_datetime, require_window
from . import raise_for_status

BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE = "offline_access Calendars.ReadWrite"

# Graph's showAs values that actually block a slot. "free" does not, and
# "workingElsewhere" means the person is still available to meet.
BLOCKING = frozenset({"busy", "oof", "tentative"})

# calendarView page size when deriving free/busy. Higher than a list_events page
# because a missed event here reads as "free" and books over a real meeting.
_FREEBUSY_PAGE = 250


class MicrosoftCalendar:
    def __init__(
        self,
        *,
        auth: RefreshTokenAuth,
        calendar_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = auth
        self._default_calendar = calendar_id
        self._client = client or httpx.AsyncClient(timeout=30.0)

    # --- plumbing ---------------------------------------------------------

    async def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._auth.access_token()}",
            # Pin the timezone Graph renders times in. Without this the naive
            # dateTime comes back in the mailbox's own zone and normalisation
            # would need a tz-database lookup per event.
            "Prefer": 'outlook.timezone="UTC"',
        }

    async def _request(self, method: str, path: str, *, context: str, **kw) -> dict:
        try:
            resp = await self._client.request(
                method, f"{BASE}{path}", headers=await self._headers(), **kw
            )
        except httpx.HTTPError as e:
            raise Unavailable(f"{context}: {type(e).__name__}") from e
        raise_for_status(resp, context)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _segment(value: str) -> str:
        """Percent-encode a user-supplied URL path segment.

        ⚠️ Graph calendar and event ids are long base64-ish strings that can
        carry '/' and '='; an id is also whatever the agent passes. Unquoted, a
        '../' segment escapes the six-verb surface into the rest of Graph under
        the same Calendars.ReadWrite grant.
        """
        return quote(value, safe="")

    def _view_path(self, calendar_id: str | None) -> str:
        cal = calendar_id or self._default_calendar
        return f"/me/calendars/{self._segment(cal)}/calendarView" if cal else "/me/calendarView"

    @staticmethod
    def _edge(node: dict) -> str:
        """Graph: {"dateTime": "2026-09-15T09:00:00.0000000", "timeZone": "UTC"}.

        The Prefer header pins timeZone to UTC, so a 'Z' suffix is correct and
        deterministic. Any other zone means the header was dropped somewhere —
        return the raw value rather than mislabel it as UTC.
        """
        raw = (node.get("dateTime") or "").split(".")[0]
        if not raw:
            return ""
        return f"{raw}Z" if node.get("timeZone") == "UTC" else raw

    @classmethod
    def _event(cls, raw: dict) -> dict:
        return {
            "id": raw.get("id", ""),
            "summary": raw.get("subject", ""),
            "start": cls._edge(raw.get("start") or {}),
            "end": cls._edge(raw.get("end") or {}),
            "location": (raw.get("location") or {}).get("displayName", ""),
            "attendees": [
                (a.get("emailAddress") or {}).get("address", "")
                for a in raw.get("attendees") or []
            ],
            "organizer": ((raw.get("organizer") or {}).get("emailAddress") or {}).get(
                "address", ""
            ),
        }

    # --- reads ------------------------------------------------------------

    async def list_calendars(self) -> dict:
        data = await self._request("GET", "/me/calendars", context="list_calendars")
        return {
            "calendars": [
                {
                    "id": c.get("id", ""),
                    "name": c.get("name", ""),
                    "primary": bool(c.get("isDefaultCalendar", False)),
                }
                for c in data.get("value") or []
            ]
        }

    async def _calendar_view(
        self, *, start: str, end: str, calendar_id: str | None, max_results: int, context: str
    ) -> dict:
        """The raw page, NOT just its items — the caller decides what a
        truncated page means, and the two callers disagree (see get_freebusy)."""
        return await self._request(
            "GET",
            self._view_path(calendar_id),
            context=context,
            params={
                "startDateTime": start,
                "endDateTime": end,
                "$orderby": "start/dateTime",
                "$top": max_results,
            },
        )

    async def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_id: str | None = None,
        max_results: int = 50,
    ) -> dict:
        require_window(start, end)
        # A truncated page is the CONTRACT here: max_results is the agent's own
        # cap ("cap on events returned"), and Google's maxResults truncates
        # identically. Refusing would fork behaviour between the two surfaces
        # over an argument the caller chose. get_freebusy is the opposite case.
        data = await self._calendar_view(
            start=start,
            end=end,
            calendar_id=calendar_id,
            max_results=max_results,
            context="list_events",
        )
        return {"events": [self._event(e) for e in data.get("value") or []]}

    async def get_freebusy(
        self, *, start: str, end: str, calendar_ids: list[str] | None = None
    ) -> dict:
        require_window(start, end)
        targets = calendar_ids or [self._default_calendar]
        busy: list[dict] = []
        for cal in targets:
            data = await self._calendar_view(
                start=start,
                end=end,
                calendar_id=cal,
                max_results=_FREEBUSY_PAGE,
                context="get_freebusy",
            )
            # ⚠️ REFUSE rather than under-report. A missed event here reads as
            # "free" and books over a real meeting, with no signal at all — and
            # nothing caps the window, so "am I free next quarter?" is a 90-day
            # request. Following the nextLink chain would answer more windows
            # but makes an unbounded number of calls behind one tool call; a
            # loud, retryable error the agent can act on is the better trade,
            # and Google needs none of this because freeBusy is server-computed.
            if data.get("@odata.nextLink"):
                raise Unavailable(
                    f"get_freebusy: the window holds more than {_FREEBUSY_PAGE} "
                    f"events, so busy time would be under-reported; narrow it "
                    f"and ask again"
                )
            for e in data.get("value") or []:
                if (e.get("showAs") or "busy").lower() not in BLOCKING:
                    continue
                busy.append(
                    {
                        "calendar_id": cal or "default",
                        "start": self._edge(e.get("start") or {}),
                        "end": self._edge(e.get("end") or {}),
                    }
                )
        return {"busy": busy}

    # --- writes -----------------------------------------------------------

    @staticmethod
    def _time(value: str) -> dict:
        """Convert to UTC and emit an OFFSET-FREE dateTime beside timeZone: UTC.

        ⚠️ Graph documents `dateTime` as an offset-free combined date-time
        interpreted in the companion `timeZone`; every documented example is
        offset-free. Passing the caller's own offset AND timeZone: "UTC" is
        undocumented, and if Graph resolves in favour of timeZone then every
        booking made from a non-UTC offset — the expected case, not the edge
        case — lands wrong by exactly that offset. That is the failure times.py
        exists to prevent, reintroduced one layer down.

        Converting first is correct under either reading, and makes the write
        path symmetric with `_edge`, which already assumes UTC because of the
        Prefer header.
        """
        utc = require_offset_datetime(value, "time").astimezone(UTC)
        return {"dateTime": utc.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}

    @staticmethod
    def _attendees(addresses: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": a}, "type": "required"} for a in addresses]

    def _events_path(self, calendar_id: str | None) -> str:
        cal = calendar_id or self._default_calendar
        return f"/me/calendars/{self._segment(cal)}/events" if cal else "/me/events"

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        calendar_id: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict:
        require_window(start, end)
        body: dict = {
            "subject": summary,
            "start": self._time(start),
            "end": self._time(end),
        }
        if description:
            body["body"] = {"contentType": "text", "content": description}
        if location:
            body["location"] = {"displayName": location}
        if attendees:
            body["attendees"] = self._attendees(attendees)

        data = await self._request(
            "POST", self._events_path(calendar_id), context="create_event", json=body
        )
        return {"event": self._event(data)}

    async def update_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict:
        body: dict = {}
        if summary is not None:
            body["subject"] = summary
        if description is not None:
            body["body"] = {"contentType": "text", "content": description}
        if location is not None:
            body["location"] = {"displayName": location}
        if attendees is not None:
            body["attendees"] = self._attendees(attendees)

        if start is not None and end is not None:
            require_window(start, end)
        if start is not None:
            require_offset_datetime(start, "start")
            body["start"] = self._time(start)
        if end is not None:
            require_offset_datetime(end, "end")
            body["end"] = self._time(end)

        if not body:
            raise UsageError("update_event: no fields to update were supplied")

        # Graph event ids are global — no calendar segment is needed, and
        # including one would 404 an event that lives in another calendar.
        # `calendar_id` is accepted and ignored so the two adapters share one
        # signature; the executor validates against one schema for both.
        data = await self._request(
            "PATCH", f"/me/events/{self._segment(event_id)}", context="update_event", json=body
        )
        return {"event": self._event(data)}

    async def delete_event(self, *, event_id: str, calendar_id: str | None = None) -> dict:
        await self._request(
            "DELETE", f"/me/events/{self._segment(event_id)}", context="delete_event"
        )
        return {"deleted": True, "event_id": event_id}

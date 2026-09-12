"""Google Calendar API v3 adapter.

⚠️ THE OAUTH CONSENT SCREEN BACKING THE REFRESH TOKEN MUST BE PUBLISHED TO
PRODUCTION. Left in "Testing", Google expires refresh tokens after SEVEN DAYS,
and the surface then dies weekly for reasons that look like anything but the
consent screen. Same failure family as the gitea-home PAT: setup passes, and the
breakage surfaces somewhere else entirely. See docs/CALENDAR-BOOTSTRAP.md.

⚠️ The OAuth client must be a DESKTOP app. A "Web application" client uses a
different redirect model and will not complete the loopback bootstrap flow.
"""

from urllib.parse import quote

import httpx

from ....errors import Unavailable, UsageError
from ..oauth import RefreshTokenAuth
from ..times import require_offset_datetime, require_window
from . import raise_for_status

BASE = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_CALENDAR = "primary"
# The broad scope, not calendar.events. Upstream does not document which scope
# freeBusy needs, and a 403 there would look exactly like a revoked token.
SCOPE = "https://www.googleapis.com/auth/calendar"


class GoogleCalendar:
    def __init__(
        self,
        *,
        auth: RefreshTokenAuth,
        calendar_id: str = DEFAULT_CALENDAR,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = auth
        self._default_calendar = calendar_id
        self._client = client or httpx.AsyncClient(timeout=30.0)

    # --- plumbing ---------------------------------------------------------

    async def _headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._auth.access_token()}"}

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

        ⚠️ Google's secondary calendar ids genuinely contain '#' and '@'
        (en.usa#holiday@group.v.calendar.google.com), and list_calendars hands
        exactly those ids to the agent. Unquoted, the '#' truncates the path at
        the fragment; a '../' segment is worse still — the grant here is the
        broad calendar scope, so a traversal reaches the whole Calendar API and
        dissolves the six-verb list that IS this surface's boundary.
        """
        return quote(value, safe="")

    def _calendar(self, calendar_id: str | None) -> str:
        return self._segment(calendar_id or self._default_calendar)

    @staticmethod
    def _edge(node: dict) -> str:
        """A Google event start/end is `dateTime` (timed) or `date` (all-day)."""
        return node.get("dateTime") or node.get("date", "")

    @classmethod
    def _event(cls, raw: dict) -> dict:
        return {
            "id": raw.get("id", ""),
            "summary": raw.get("summary", ""),
            "start": cls._edge(raw.get("start") or {}),
            "end": cls._edge(raw.get("end") or {}),
            "location": raw.get("location", ""),
            "attendees": [a.get("email", "") for a in raw.get("attendees") or []],
            "organizer": (raw.get("organizer") or {}).get("email", ""),
        }

    # --- reads ------------------------------------------------------------

    async def list_calendars(self) -> dict:
        data = await self._request("GET", "/users/me/calendarList", context="list_calendars")
        return {
            "calendars": [
                {
                    "id": c.get("id", ""),
                    "name": c.get("summary", ""),
                    "primary": bool(c.get("primary", False)),
                }
                for c in data.get("items") or []
            ]
        }

    async def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_id: str | None = None,
        max_results: int = 50,
    ) -> dict:
        require_window(start, end)
        data = await self._request(
            "GET",
            f"/calendars/{self._calendar(calendar_id)}/events",
            context="list_events",
            params={
                "timeMin": start,
                "timeMax": end,
                # Expand recurring series into occurrences. Without this a weekly
                # standup is ONE result carrying an RRULE, and every availability
                # calculation built on it is wrong.
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max_results,
            },
        )
        return {"events": [self._event(e) for e in data.get("items") or []]}

    async def get_freebusy(
        self, *, start: str, end: str, calendar_ids: list[str] | None = None
    ) -> dict:
        require_window(start, end)
        ids = calendar_ids or [self._default_calendar]
        data = await self._request(
            "POST",
            "/freeBusy",
            context="get_freebusy",
            json={"timeMin": start, "timeMax": end, "items": [{"id": i} for i in ids]},
        )
        busy: list[dict] = []
        for cal_id, node in (data.get("calendars") or {}).items():
            # ⚠️ Google reports a bad calendar id INSIDE a 200 response, with an
            # empty busy list beside it. Treating that as "free" would let an
            # agent book straight over a real meeting, so it is an error here.
            errors = node.get("errors")
            if errors:
                reasons = ", ".join(e.get("reason", "?") for e in errors)
                raise Unavailable(f"get_freebusy: calendar '{cal_id}' failed: {reasons}")
            for interval in node.get("busy") or []:
                busy.append(
                    {
                        "calendar_id": cal_id,
                        "start": interval.get("start", ""),
                        "end": interval.get("end", ""),
                    }
                )
        return {"busy": busy}

    # --- writes -----------------------------------------------------------

    # Google records attendees but notifies nobody unless asked. An invitation
    # nobody receives is the most confusing possible outcome of "book a meeting".
    _SEND_UPDATES = {"sendUpdates": "all"}  # noqa: RUF012

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
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]

        data = await self._request(
            "POST",
            f"/calendars/{self._calendar(calendar_id)}/events",
            context="create_event",
            params=self._SEND_UPDATES,
            json=body,
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
        # PATCH, not PUT: only the supplied fields change. A PUT would silently
        # clear every field the caller did not resend.
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if attendees is not None:
            body["attendees"] = [{"email": a} for a in attendees]

        if start is not None and end is not None:
            require_window(start, end)
        if start is not None:
            require_offset_datetime(start, "start")
            body["start"] = {"dateTime": start}
        if end is not None:
            require_offset_datetime(end, "end")
            body["end"] = {"dateTime": end}

        if not body:
            raise UsageError("update_event: no fields to update were supplied")

        data = await self._request(
            "PATCH",
            f"/calendars/{self._calendar(calendar_id)}/events/{self._segment(event_id)}",
            context="update_event",
            params=self._SEND_UPDATES,
            json=body,
        )
        return {"event": self._event(data)}

    async def delete_event(self, *, event_id: str, calendar_id: str | None = None) -> dict:
        await self._request(
            "DELETE",
            f"/calendars/{self._calendar(calendar_id)}/events/{self._segment(event_id)}",
            context="delete_event",
            params=self._SEND_UPDATES,
        )
        return {"deleted": True, "event_id": event_id}

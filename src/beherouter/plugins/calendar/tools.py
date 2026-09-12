"""The calendar surface: six tools, provider-independent.

Deliberately small. The two upstream servers this replaces publish 12 and 300+
tools respectively, almost all of it mail, files, contacts and tasks. These six
are the complete book-and-adjust loop:

    list_calendars -> get_freebusy -> create_event   (book)
    list_events    -> update_event / delete_event    (adjust)

`get_freebusy` is the one that makes this worth building. Without it an agent
guesses a time and creates conflicts.

There is deliberately NO get_current_time: the surface's instructions carry the
clock, and a tool round-trip to read one is waste.

⚠️ `descriptors_for` returns FRESH objects every call. Two calendar surfaces run
in one process and `surface.build_surface` reads `descriptor.pinned`, so handing
out a shared module-level list would let one surface's pin overrides silently
change the other's advertised tools.
"""

import copy
from collections.abc import Iterable

from ...models import ToolDescriptor

VERBS: tuple[str, ...] = (
    "list_calendars",
    "list_events",
    "get_freebusy",
    "create_event",
    "update_event",
    "delete_event",
)

MUTATING: frozenset[str] = frozenset({"create_event", "update_event", "delete_event"})

_OFFSET_HINT = (
    "RFC 3339 timestamp WITH an explicit UTC offset, e.g. 2026-09-15T14:00:00+06:00 "
    "or 2026-09-15T08:00:00Z. A timestamp without an offset is rejected."
)

_CALENDAR_ID = {
    "type": "string",
    "description": "Calendar id from list_calendars. Omit to use the account's default calendar.",
}

_ATTENDEES = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Attendee email addresses. The provider sends the invitations.",
}

SUMMARIES: dict[str, str] = {
    "list_calendars": "List the calendars this account can see, with their ids.",
    "list_events": (
        "List events between two instants. Recurring series are expanded into "
        "individual occurrences."
    ),
    "get_freebusy": (
        "Return the BUSY intervals in a window. Use this before create_event to find "
        "a slot that is actually free instead of guessing."
    ),
    "create_event": "Book a new event and invite attendees.",
    "update_event": (
        "Change an existing event. Only the fields you pass are modified; everything "
        "else is left as it is."
    ),
    # ⚠️ Google's delete sends sendUpdates=all; Graph's DELETE /me/events/{id}
    # notifies nobody (that is the separate /cancel action, which is valid only
    # for the organizer). Promise only what BOTH providers do — an organizer
    # who cancels believing attendees were told is a failure this text caused.
    "delete_event": (
        "Cancel an event. Whether attendees are notified is provider-dependent: "
        "Google notifies them, Microsoft does not."
    ),
}

SCHEMAS: dict[str, dict] = {
    "list_calendars": {"type": "object", "properties": {}, "required": []},
    "list_events": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": f"Window start. {_OFFSET_HINT}"},
            "end": {"type": "string", "description": f"Window end. {_OFFSET_HINT}"},
            "calendar_id": _CALENDAR_ID,
            "max_results": {
                "type": "integer",
                "description": "Cap on events returned.",
            },
        },
        "required": ["start", "end"],
    },
    "get_freebusy": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": f"Window start. {_OFFSET_HINT}"},
            "end": {"type": "string", "description": f"Window end. {_OFFSET_HINT}"},
            "calendar_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Calendars to check. Omit for the default calendar.",
            },
        },
        "required": ["start", "end"],
    },
    "create_event": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": f"Event start. {_OFFSET_HINT}"},
            "end": {"type": "string", "description": f"Event end. {_OFFSET_HINT}"},
            "calendar_id": _CALENDAR_ID,
            "description": {"type": "string", "description": "Body text / agenda."},
            "location": {"type": "string", "description": "Free-text location."},
            "attendees": _ATTENDEES,
        },
        "required": ["summary", "start", "end"],
    },
    "update_event": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Event id from list_events."},
            "calendar_id": _CALENDAR_ID,
            "summary": {"type": "string", "description": "New title."},
            "start": {"type": "string", "description": f"New start. {_OFFSET_HINT}"},
            "end": {"type": "string", "description": f"New end. {_OFFSET_HINT}"},
            "description": {"type": "string", "description": "New body text."},
            "location": {"type": "string", "description": "New location."},
            "attendees": _ATTENDEES,
        },
        "required": ["event_id"],
    },
    "delete_event": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Event id from list_events."},
            "calendar_id": _CALENDAR_ID,
        },
        "required": ["event_id"],
    },
}


def descriptors_for(pinned: Iterable[str]) -> list[ToolDescriptor]:
    """Build a fresh descriptor list, pinning the named verbs."""
    names = set(pinned)
    return [
        ToolDescriptor(
            name=verb,
            verb=verb,
            summary=SUMMARIES[verb],
            schema=copy.deepcopy(SCHEMAS[verb]),
            pinned=verb in names,
            mutating=verb in MUTATING,
        )
        for verb in VERBS
    ]

"""One time rule, enforced at the tool boundary.

Every timestamp crossing into this plugin is RFC 3339 WITH an explicit UTC
offset. Both provider APIs happily accept a naive local time plus a separate
timeZone field, so a model that omits the zone books a real meeting at the wrong
hour and a human finds out days later. A loud, retryable error beats that.
"""

from datetime import datetime

from ...errors import UsageError


def require_offset_datetime(value: str, field: str) -> datetime:
    """Parse an RFC 3339 timestamp, rejecting anything without an offset."""
    if not isinstance(value, str):
        raise UsageError(f"'{field}' must be an RFC 3339 string, got {type(value).__name__}")
    try:
        # fromisoformat handles the trailing 'Z' from Python 3.11 onward.
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise UsageError(
            f"'{field}' is not a valid RFC 3339 timestamp: {value!r}. "
            f"Expected e.g. 2026-09-15T14:00:00+06:00"
        ) from e
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UsageError(
            f"'{field}' must include a UTC offset: {value!r}. "
            f"Expected e.g. 2026-09-15T14:00:00+06:00 or 2026-09-15T08:00:00Z. "
            f"Booking without an offset silently uses the calendar's own timezone."
        )
    return parsed


def require_window(start: str, end: str) -> tuple[datetime, datetime]:
    """Parse both ends of a time window and assert it runs forwards."""
    s = require_offset_datetime(start, "start")
    e = require_offset_datetime(end, "end")
    if e <= s:
        raise UsageError(f"end must be after start: start={start!r} end={end!r}")
    return s, e

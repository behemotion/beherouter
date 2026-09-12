from datetime import timedelta

import pytest

from beherouter.errors import UsageError
from beherouter.plugins.calendar.times import require_offset_datetime, require_window


def test_accepts_an_explicit_positive_offset():
    dt = require_offset_datetime("2026-09-15T14:00:00+06:00", "start")
    assert dt.utcoffset() == timedelta(hours=6)


def test_accepts_a_z_suffix():
    dt = require_offset_datetime("2026-09-15T08:00:00Z", "start")
    assert dt.utcoffset() == timedelta(0)


def test_rejects_a_naive_datetime():
    with pytest.raises(UsageError, match="must include a UTC offset"):
        require_offset_datetime("2026-09-15T14:00:00", "start")


def test_rejects_a_bare_date():
    with pytest.raises(UsageError, match="must include a UTC offset"):
        require_offset_datetime("2026-09-15", "start")


def test_rejects_unparseable_text():
    with pytest.raises(UsageError, match="not a valid RFC 3339"):
        require_offset_datetime("next tuesday", "start")


def test_rejects_a_non_string():
    with pytest.raises(UsageError, match="must be an RFC 3339 string"):
        require_offset_datetime(1758000000, "start")


def test_error_names_the_offending_field():
    with pytest.raises(UsageError, match="'end'"):
        require_offset_datetime("nonsense", "end")


def test_window_returns_both_ends():
    s, e = require_window("2026-09-15T14:00:00+06:00", "2026-09-15T15:00:00+06:00")
    assert (e - s) == timedelta(hours=1)


def test_window_rejects_end_before_start():
    with pytest.raises(UsageError, match="end must be after start"):
        require_window("2026-09-15T15:00:00+06:00", "2026-09-15T14:00:00+06:00")


def test_window_rejects_a_zero_length_window():
    with pytest.raises(UsageError, match="end must be after start"):
        require_window("2026-09-15T14:00:00+06:00", "2026-09-15T14:00:00+06:00")


def test_window_compares_across_different_offsets():
    """08:00Z is BEFORE 14:00+06:00 only if the offsets are honoured."""
    with pytest.raises(UsageError, match="end must be after start"):
        require_window("2026-09-15T14:00:00+06:00", "2026-09-15T08:00:00Z")

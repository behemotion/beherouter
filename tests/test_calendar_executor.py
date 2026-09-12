import pytest

from beherouter.errors import UsageError
from beherouter.plugins.calendar import build_backend
from beherouter.plugins.calendar.executor import CalendarExecutor


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def list_calendars(self, **kw):
        self.calls.append(("list_calendars", kw))
        return {"calendars": []}

    async def list_events(self, **kw):
        self.calls.append(("list_events", kw))
        return {"events": []}

    async def create_event(self, **kw):
        self.calls.append(("create_event", kw))
        return {"event": {"id": "x"}}


async def test_unknown_verb_is_a_usage_error():
    with pytest.raises(UsageError, match="unknown verb"):
        await CalendarExecutor(FakeProvider()).run("send_email", {})


async def test_unknown_argument_is_a_usage_error():
    """A typo'd argument must not be forwarded as a silent no-op."""
    with pytest.raises(UsageError, match="unknown argument"):
        await CalendarExecutor(FakeProvider()).run("list_calendars", {"calender_id": "typo"})


async def test_missing_required_argument_is_a_usage_error():
    with pytest.raises(UsageError, match="missing required argument"):
        await CalendarExecutor(FakeProvider()).run("create_event", {"summary": "x"})


async def test_run_dispatches_to_the_provider():
    p = FakeProvider()
    out = await CalendarExecutor(p).run(
        "list_events", {"start": "2026-09-15T00:00:00Z", "end": "2026-09-16T00:00:00Z"}
    )
    assert out == {"events": []}
    assert p.calls[0][1]["start"] == "2026-09-15T00:00:00Z"


async def test_run_injects_the_configured_max_results():
    p = FakeProvider()
    await CalendarExecutor(p, max_results=7).run(
        "list_events", {"start": "2026-09-15T00:00:00Z", "end": "2026-09-16T00:00:00Z"}
    )
    assert p.calls[0][1]["max_results"] == 7


async def test_an_explicit_max_results_wins():
    p = FakeProvider()
    await CalendarExecutor(p, max_results=7).run(
        "list_events",
        {"start": "2026-09-15T00:00:00Z", "end": "2026-09-16T00:00:00Z", "max_results": 3},
    )
    assert p.calls[0][1]["max_results"] == 3


async def test_max_results_is_not_injected_into_other_verbs():
    p = FakeProvider()
    await CalendarExecutor(p, max_results=7).run("list_calendars", {})
    assert p.calls[0][1] == {}


def test_build_backend_produces_a_native_backend():
    b = build_backend(surface="gcal", provider=FakeProvider(), pinned=["list_calendars"])
    assert b.name == "gcal"
    assert b.kind == "native"
    assert len(b.descriptors) == 6
    assert [d.name for d in b.pinned] == ["list_calendars"]


def test_build_backend_does_not_share_descriptors_between_surfaces():
    a = build_backend(surface="gcal", provider=FakeProvider(), pinned=["list_calendars"])
    b = build_backend(surface="m365", provider=FakeProvider(), pinned=["list_events"])
    assert [d.name for d in a.pinned] == ["list_calendars"]
    assert [d.name for d in b.pinned] == ["list_events"]


async def test_wrong_typed_array_argument_is_a_usage_error():
    """A wrong-typed run_tool argument must be a UsageError, not a TypeError."""
    with pytest.raises(UsageError, match="must be list"):
        await CalendarExecutor(FakeProvider()).run(
            "create_event",
            {
                "summary": "x",
                "start": "2026-09-15T00:00:00Z",
                "end": "2026-09-16T00:00:00Z",
                "attendees": 5,
            },
        )


async def test_wrong_typed_string_argument_is_a_usage_error():
    with pytest.raises(UsageError, match="must be str"):
        await CalendarExecutor(FakeProvider()).run(
            "create_event",
            {"summary": 5, "start": "2026-09-15T00:00:00Z", "end": "2026-09-16T00:00:00Z"},
        )


async def test_bool_is_rejected_for_an_integer_field():
    """bool is a subclass of int in Python; True must not satisfy an integer field."""
    with pytest.raises(UsageError, match="must be int"):
        await CalendarExecutor(FakeProvider()).run(
            "list_events",
            {
                "start": "2026-09-15T00:00:00Z",
                "end": "2026-09-16T00:00:00Z",
                "max_results": True,
            },
        )


async def test_correctly_typed_call_still_dispatches():
    p = FakeProvider()
    out = await CalendarExecutor(p).run(
        "create_event",
        {
            "summary": "x",
            "start": "2026-09-15T00:00:00Z",
            "end": "2026-09-16T00:00:00Z",
            "attendees": ["a@example.com"],
        },
    )
    assert out == {"event": {"id": "x"}}
    assert p.calls[0][1]["attendees"] == ["a@example.com"]


# --- the AxiError funnel -----------------------------------------------------


class BrokenProvider:
    """Whatever a provider does wrong, it must reach the gateway as an AxiError."""

    async def list_calendars(self, **kw):
        # Exactly what resp.json() raises on a 2xx with a non-JSON body.
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    async def list_events(self, **kw):
        raise UsageError("end must be after start")


async def test_a_non_axierror_from_the_provider_becomes_unavailable():
    """A bare ValueError is not an AxiError: it escapes health.check_entry and
    tracebacks the whole `health --deep` run, losing every other backend's
    state instead of reporting this one as failed."""
    from beherouter.errors import Unavailable

    with pytest.raises(Unavailable, match="'list_calendars' failed: ValueError"):
        await CalendarExecutor(BrokenProvider()).run("list_calendars", {})


async def test_the_funnel_never_interpolates_the_exception():
    """⚠️ A blanket handler that formatted {e} would re-open the leak oauth.py
    closes: an httpx error carries its request, and the refresh grant's form
    body with it."""
    from beherouter.errors import Unavailable

    class Leaky:
        async def list_calendars(self, **kw):
            raise RuntimeError("refresh_token=SUPERSECRET")

    with pytest.raises(Unavailable) as ei:
        await CalendarExecutor(Leaky()).run("list_calendars", {})
    assert "SUPERSECRET" not in str(ei.value)


async def test_an_axierror_passes_through_unwrapped():
    """The funnel must not turn a precise UsageError into a vague Unavailable."""
    with pytest.raises(UsageError, match="end must be after start"):
        await CalendarExecutor(BrokenProvider()).run(
            "list_events", {"start": "2026-09-15T00:00:00Z", "end": "2026-09-16T00:00:00Z"}
        )

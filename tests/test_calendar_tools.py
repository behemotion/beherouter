from beherouter.plugins.calendar.tools import (
    MUTATING,
    SCHEMAS,
    SUMMARIES,
    VERBS,
    descriptors_for,
)

ALL = list(VERBS)


def test_exposes_exactly_the_six_booking_verbs():
    assert set(VERBS) == {
        "list_calendars",
        "list_events",
        "get_freebusy",
        "create_event",
        "update_event",
        "delete_event",
    }


def test_write_verbs_are_marked_mutating():
    assert {d.name for d in descriptors_for(ALL) if d.mutating} == {
        "create_event",
        "update_event",
        "delete_event",
    }


def test_descriptors_for_pins_only_the_named_tools():
    ds = descriptors_for(["list_calendars", "list_events"])
    assert {d.name for d in ds if d.pinned} == {"list_calendars", "list_events"}
    assert len(ds) == 6


def test_descriptors_are_not_shared_between_calls():
    """Two surfaces live in one process; a shared list would leak pin overrides."""
    a = descriptors_for(["list_calendars"])
    b = descriptors_for(ALL)
    assert a[0] is not b[0]
    assert a[0].schema is not b[0].schema


def test_nested_schema_objects_are_copied_too():
    """SCHEMAS shares the _CALENDAR_ID and _ATTENDEES dicts ACROSS entries, so
    the deepcopy in descriptors_for is the only thing isolating one surface's
    schemas from the other's — and from the module constant. A shallow copy
    would still pass the top-level identity checks above."""
    a = next(d for d in descriptors_for(ALL) if d.name == "create_event")
    b = next(d for d in descriptors_for(ALL) if d.name == "create_event")
    assert a.schema["properties"]["calendar_id"] is not b.schema["properties"]["calendar_id"]

    a.schema["properties"]["calendar_id"]["description"] = "mutated"
    assert b.schema["properties"]["calendar_id"]["description"] != "mutated"
    assert SCHEMAS["create_event"]["properties"]["calendar_id"]["description"] != "mutated"
    assert SCHEMAS["update_event"]["properties"]["calendar_id"]["description"] != "mutated"


def test_every_schema_is_a_json_schema_object():
    for name, schema in SCHEMAS.items():
        assert schema["type"] == "object", name
        assert "properties" in schema, name
        assert isinstance(schema.get("required", []), list), name


def test_list_calendars_takes_no_required_arguments():
    """It doubles as the credential probe, which health --deep calls with no args."""
    assert SCHEMAS["list_calendars"]["required"] == []


def test_time_arguments_document_the_offset_requirement():
    for name in ("list_events", "get_freebusy", "create_event"):
        assert "offset" in SCHEMAS[name]["properties"]["start"]["description"], name


def test_create_event_requires_summary_start_and_end():
    assert set(SCHEMAS["create_event"]["required"]) == {"summary", "start", "end"}


def test_update_and_delete_require_an_event_id():
    for name in ("update_event", "delete_event"):
        assert "event_id" in SCHEMAS[name]["required"], name


def test_names_and_verbs_match():
    """Native plugins publish flat names directly; there is no verb flattening."""
    assert all(d.name == d.verb for d in descriptors_for(ALL))


def test_mutating_and_verbs_agree():
    assert set(VERBS) >= MUTATING


def test_delete_event_promises_only_what_both_providers_do():
    """Google's delete notifies attendees; Graph's DELETE does not (that is the
    separate /cancel action). An organizer who cancels through m365 believing
    attendees were told is a real failure the tool description would cause."""
    summary = SUMMARIES["delete_event"]
    assert "provider-dependent" in summary
    assert "The provider notifies attendees." not in summary


def test_both_providers_share_one_http_plumbing():
    """Audit §2.3: `__init__`, `_request` and `_segment` were byte-identical in
    both adapters. One copy, so a fix to the error funnel or the path escaping
    cannot land in one provider and not the other."""
    from beherouter.plugins.calendar.providers import HttpCalendarProvider
    from beherouter.plugins.calendar.providers.google import GoogleCalendar
    from beherouter.plugins.calendar.providers.microsoft import MicrosoftCalendar

    for cls in (GoogleCalendar, MicrosoftCalendar):
        assert issubclass(cls, HttpCalendarProvider)
        for name in ("__init__", "_request", "_segment", "aclose"):
            assert name not in vars(cls), f"{cls.__name__} re-defines {name}"
    assert GoogleCalendar.BASE.startswith("https://www.googleapis.com/")
    assert MicrosoftCalendar.BASE.startswith("https://graph.microsoft.com/")


async def test_a_provider_closes_its_own_clients_and_no_one_elses():
    """Each provider (and so each credential) owns its clients: closing the
    Google surface's provider must not touch anything Microsoft holds."""
    import httpx

    from beherouter.plugins.calendar.oauth import RefreshTokenAuth
    from beherouter.plugins.calendar.providers.google import GoogleCalendar
    from beherouter.plugins.calendar.providers.microsoft import MicrosoftCalendar

    def make(cls):
        auth = RefreshTokenAuth(token_url="https://t/token", client_id="c", refresh_token="r")
        return cls(auth=auth), auth

    g, g_auth = make(GoogleCalendar)
    m, m_auth = make(MicrosoftCalendar)
    await g.aclose()
    assert g._client.is_closed and g_auth._client.is_closed
    assert not m._client.is_closed and not m_auth._client.is_closed
    await m.aclose()
    assert isinstance(m._client, httpx.AsyncClient) and m._client.is_closed


async def test_the_executor_closes_every_provider_it_holds():
    from beherouter.plugins.calendar.executor import CalendarExecutor

    closed = []

    class P:
        def __init__(self, name):
            self.name = name

        async def list_calendars(self):
            return {"calendars": []}

        async def aclose(self):
            closed.append(self.name)

    ident = type("I", (), {"credentials": {"refresh_token": "x"}, "cache_key": "k"})()
    ex = CalendarExecutor(P("deploy"), provider_factory=lambda creds: P("alice"))
    await ex.run("list_calendars", {}, identity=ident)
    await ex.aclose()
    assert sorted(closed) == ["alice", "deploy"]

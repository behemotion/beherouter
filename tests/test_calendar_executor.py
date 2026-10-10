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


class _FakeProvider:
    def __init__(self, token):
        self.token = token

    async def list_calendars(self):
        return {"token": self.token}


def _counting_factory(built):
    def factory(creds):
        built.append(creds["refresh_token"])
        return _FakeProvider(creds["refresh_token"])

    return factory


async def test_each_identity_gets_its_own_provider():
    from beherouter.identity import CallIdentity
    from beherouter.plugins.calendar.executor import CalendarExecutor

    built = []
    factory = _counting_factory(built)
    ex = CalendarExecutor(
        factory({"refresh_token": "deployment"}), provider_factory=factory
    )
    alice = CallIdentity(
        subject="alice", credentials={"refresh_token": "rt-alice"}, cache_key="a"
    )
    bob = CallIdentity(
        subject="bob", credentials={"refresh_token": "rt-bob"}, cache_key="b"
    )
    assert (await ex.run("list_calendars", {}, identity=alice))["token"] == "rt-alice"
    assert (await ex.run("list_calendars", {}, identity=bob))["token"] == "rt-bob"
    # alice again: served from the cache, not rebuilt
    assert (await ex.run("list_calendars", {}, identity=alice))["token"] == "rt-alice"
    assert built == ["deployment", "rt-alice", "rt-bob"]


async def test_the_provider_cache_is_bounded():
    from beherouter.identity import CallIdentity
    from beherouter.plugins.calendar.executor import CalendarExecutor

    built = []
    factory = _counting_factory(built)
    ex = CalendarExecutor(
        factory({"refresh_token": "d"}), provider_factory=factory, cache_size=2
    )
    for name in ("a", "b", "c", "a"):
        await ex.run(
            "list_calendars",
            {},
            identity=CallIdentity(
                subject=name, credentials={"refresh_token": name}, cache_key=name
            ),
        )
    # 'a' was evicted by 'c' and rebuilt: 1 deployment + 3 + 1
    assert built == ["d", "a", "b", "c", "a"]


async def test_no_identity_uses_the_deployment_provider():
    from beherouter.plugins.calendar.executor import CalendarExecutor

    class DeploymentProvider:
        async def list_calendars(self):
            return {"who": "deployment"}

    ex = CalendarExecutor(DeploymentProvider())
    assert (await ex.run("list_calendars", {}))["who"] == "deployment"


# --- eviction closes providers, but never one that is mid-call ---------------


class _ClosingProvider:
    """Records aclose(); list_calendars may be held open on an Event."""

    def __init__(self, token, gate=None):
        self.token = token
        self.gate = gate
        self.closed = 0
        self.started = None

    async def list_calendars(self):
        if self.gate is not None:
            self.started.set()
            await self.gate.wait()
        assert not self.closed, "a provider was closed while serving a call"
        return {"token": self.token}

    async def aclose(self):
        self.closed += 1


def _who(name):
    from beherouter.identity import CallIdentity

    return CallIdentity(subject=name, credentials={"refresh_token": name}, cache_key=name)


async def test_an_evicted_idle_provider_is_closed():
    """Two httpx clients leak per evicted user unless eviction closes them."""
    built: dict[str, _ClosingProvider] = {}

    def factory(creds):
        built[creds["refresh_token"]] = _ClosingProvider(creds["refresh_token"])
        return built[creds["refresh_token"]]

    ex = CalendarExecutor(_ClosingProvider("d"), provider_factory=factory, cache_size=1)
    await ex.run("list_calendars", {}, identity=_who("a"))
    await ex.run("list_calendars", {}, identity=_who("b"))
    assert built["a"].closed == 1
    assert built["b"].closed == 0


async def test_an_evicted_provider_is_closed_only_after_its_call_finishes():
    import asyncio

    gate, started = asyncio.Event(), asyncio.Event()
    built: dict[str, _ClosingProvider] = {}

    def factory(creds):
        name = creds["refresh_token"]
        p = _ClosingProvider(name, gate=gate if name == "a" else None)
        p.started = started
        built[name] = p
        return p

    ex = CalendarExecutor(_ClosingProvider("d"), provider_factory=factory, cache_size=1)
    slow = asyncio.create_task(ex.run("list_calendars", {}, identity=_who("a")))
    await started.wait()
    # b evicts a while a's call is still in flight
    assert (await ex.run("list_calendars", {}, identity=_who("b")))["token"] == "b"
    assert built["a"].closed == 0
    gate.set()
    assert (await slow)["token"] == "a"
    assert built["a"].closed == 1


async def test_a_failing_close_does_not_fail_the_call():
    class BadClose(_ClosingProvider):
        async def aclose(self):
            raise RuntimeError("boom")

    ex = CalendarExecutor(
        _ClosingProvider("d"),
        provider_factory=lambda creds: BadClose(creds["refresh_token"]),
        cache_size=1,
    )
    await ex.run("list_calendars", {}, identity=_who("a"))
    assert (await ex.run("list_calendars", {}, identity=_who("b")))["token"] == "b"


async def test_aclose_also_closes_retired_providers():
    import asyncio

    gate, started = asyncio.Event(), asyncio.Event()
    built: dict[str, _ClosingProvider] = {}

    def factory(creds):
        name = creds["refresh_token"]
        p = _ClosingProvider(name, gate=gate if name == "a" else None)
        p.started = started
        built[name] = p
        return p

    deploy = _ClosingProvider("d")
    ex = CalendarExecutor(deploy, provider_factory=factory, cache_size=1)
    slow = asyncio.create_task(ex.run("list_calendars", {}, identity=_who("a")))
    await started.wait()
    await ex.run("list_calendars", {}, identity=_who("b"))
    await ex.aclose()
    assert (deploy.closed, built["a"].closed, built["b"].closed) == (1, 1, 1)
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    # the in-flight call's release must not close it a second time
    assert built["a"].closed == 1


async def test_a_per_user_only_surface_refuses_a_call_without_identity():
    ex = CalendarExecutor(None, provider_factory=lambda creds: FakeProvider())
    with pytest.raises(UsageError, match="requires a per-user identity"):
        await ex.run("list_calendars", {})


async def test_an_identity_without_a_factory_is_refused():
    with pytest.raises(UsageError, match="no provider factory"):
        await CalendarExecutor(FakeProvider()).run("list_calendars", {}, identity=_who("a"))


async def test_concurrent_calls_share_one_provider_until_both_finish():
    import asyncio

    gate = asyncio.Event()
    provider = _ClosingProvider("d", gate=gate)
    provider.started = asyncio.Event()
    ex = CalendarExecutor(provider)
    first = asyncio.create_task(ex.run("list_calendars", {}))
    second = asyncio.create_task(ex.run("list_calendars", {}))
    await provider.started.wait()
    await asyncio.sleep(0)
    gate.set()
    assert [r["token"] for r in await asyncio.gather(first, second)] == ["d", "d"]
    assert ex._inflight == {}


async def test_aclose_skips_a_provider_without_aclose():
    ex = CalendarExecutor(FakeProvider())
    await ex.aclose()


async def test_an_untyped_schema_property_is_not_type_checked(monkeypatch):
    from beherouter.plugins.calendar import executor

    monkeypatch.setitem(executor.SCHEMAS, "list_calendars",
                        {"properties": {"anything": {}}, "required": []})

    class Provider:
        async def list_calendars(self, anything):
            return {"got": anything}

    assert (await CalendarExecutor(Provider()).run(
        "list_calendars", {"anything": 3}))["got"] == 3

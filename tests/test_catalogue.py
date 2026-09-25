import asyncio

from beherouter.catalogue import (
    STATUS_DRIFT,
    STATUS_NONE,
    STATUS_OK,
    STATUS_STALE,
    Catalogue,
    Drift,
)
from beherouter.models import ToolDescriptor


def _d(name, pinned=False):
    return ToolDescriptor(
        name=name, verb=name, summary=f"does {name}", schema={},
        pinned=pinned, mutating=None,
    )


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_a_catalogue_without_a_relister_never_refreshes():
    """cli and native backings must be untouched by this phase."""
    cat = Catalogue([_d("a")])
    assert cat.status == STATUS_NONE


async def test_no_relister_means_ensure_fresh_is_a_no_op():
    cat = Catalogue([_d("a")])
    await cat.ensure_fresh()
    assert [d.name for d in cat.descriptors] == ["a"]
    assert cat.status == STATUS_NONE


async def test_within_the_ttl_nothing_is_refetched():
    calls = []

    async def relist():
        calls.append(1)
        return [_d("a"), _d("b")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(0.5)
    await cat.ensure_fresh()
    assert calls == []
    assert [d.name for d in cat.descriptors] == ["a"]


async def test_past_the_ttl_the_catalogue_refetches_and_reindexes():
    async def relist():
        return [_d("a"), _d("cycle_create")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert "cycle_create" in cat.by_name
    assert cat.index.search("cycle") == ["cycle_create"]


async def test_added_and_removed_are_reported_as_drift():
    async def relist():
        return [_d("a"), _d("new_one")]

    clock = _Clock()
    cat = Catalogue([_d("a"), _d("gone")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.status == STATUS_DRIFT
    assert cat.drift.added == ("new_one",)
    assert cat.drift.removed == ("gone",)
    assert cat.drift.changed is True


async def test_drift_is_logged_even_if_nobody_polls_context_cost(caplog):
    """added/removed are measured against the PREVIOUS refresh, so an
    unobserved drift reverts to `ok` on the next refresh and leaves no other
    trace -- the log line is what survives that window."""
    async def relist():
        return [_d("a"), _d("new_one")]

    clock = _Clock()
    cat = Catalogue(
        [_d("a"), _d("gone")], relist=relist, ttl_ms=1000, clock=clock,
        name="myservice",
    )
    clock.advance(2.0)
    with caplog.at_level("WARNING"):
        await cat.ensure_fresh()
    assert "myservice" in caplog.text
    assert "new_one" in caplog.text
    assert "gone" in caplog.text


async def test_an_unchanged_refresh_logs_nothing(caplog):
    async def relist():
        return [_d("a")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock, name="myservice")
    clock.advance(2.0)
    with caplog.at_level("WARNING"):
        await cat.ensure_fresh()
    assert cat.status == STATUS_OK
    assert caplog.text == ""


async def test_an_unchanged_catalogue_is_ok_not_drift():
    async def relist():
        return [_d("a")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.status == STATUS_OK
    assert cat.drift.changed is False


async def test_a_vanished_pinned_tool_is_reported_separately():
    """A pinned tool the backend no longer serves is a BROKEN PUBLISHED TOOL,
    not ordinary drift. Task 12 makes it a health failure."""
    async def relist():
        return [_d("a")]

    clock = _Clock()
    cat = Catalogue(
        [_d("a"), _d("workitem_type", pinned=True)],
        relist=relist, ttl_ms=1000, clock=clock,
    )
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.drift.pinned_missing == ("workitem_type",)


async def test_a_failed_refresh_serves_the_last_good_catalogue():
    """Trading a stale index for a dead surface is the wrong direction --
    the same instinct as /healthz not fanning out to backends."""
    async def relist():
        raise RuntimeError("socket closed")

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert [d.name for d in cat.descriptors] == ["a"]
    assert cat.status == STATUS_STALE
    assert cat.index.search("does a") == ["a"]


async def test_a_failed_refresh_does_not_retry_on_every_call():
    """A dead backend must not turn every search_tools call into a timeout."""
    calls = []

    async def relist():
        calls.append(1)
        raise RuntimeError("socket closed")

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    await cat.ensure_fresh()
    assert len(calls) == 1
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert len(calls) == 2


async def test_a_recovered_backend_leaves_stale():
    state = {"fail": True}

    async def relist():
        if state["fail"]:
            raise RuntimeError("socket closed")
        return [_d("a")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.status == STATUS_STALE
    state["fail"] = False
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.status == STATUS_OK


async def test_ttl_zero_disables_refresh_entirely():
    calls = []

    async def relist():
        calls.append(1)
        return [_d("a")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=0, clock=clock)
    clock.advance(10_000)
    await cat.ensure_fresh()
    assert calls == []
    assert cat.status == STATUS_NONE


def test_drift_changed_ignores_nothing_relevant():
    assert Drift((), (), ()).changed is False
    assert Drift(("a",), (), ()).changed is True
    assert Drift((), ("a",), ()).changed is True
    assert Drift((), (), ("a",)).changed is True


async def test_a_concurrent_refresh_relists_once_and_keeps_the_drift():
    """Two callers arriving past the TTL must not both fire a re-list.

    The duplicated round trip is the cheap half. The sharp half is that the
    second caller's `_diff` would run against the list the first one already
    installed -- seeing no change, and overwriting a genuine `drift` with `ok`
    before anyone has read it. Four meta-tools sit on this path.
    """
    calls = []

    async def relist():
        calls.append(1)
        await asyncio.sleep(0)  # yield, so the second caller enters meanwhile
        return [_d("a"), _d("new_one")]

    clock = _Clock()
    cat = Catalogue([_d("a")], relist=relist, ttl_ms=1000, clock=clock)
    clock.advance(2.0)
    await asyncio.gather(cat.ensure_fresh(), cat.ensure_fresh())
    assert calls == [1]
    assert cat.status == STATUS_DRIFT
    assert cat.drift.added == ("new_one",)


async def test_aliases_survive_a_refresh():
    async def relist():
        return [_d("cycle"), _d("module")]

    clock = _Clock()
    cat = Catalogue([_d("cycle")], relist=relist, ttl_ms=1000, clock=clock,
                    aliases={"cycle": ("sprint",)})
    assert cat.index.search("sprint") == ["cycle"]
    clock.advance(2.0)
    await cat.ensure_fresh()
    assert cat.index.search("sprint") == ["cycle"]

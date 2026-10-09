"""Pure pieces of the reload (spec §2.2, §2.3 step 2)."""

import asyncio
import os
from pathlib import Path

import pytest

from beherouter.errors import UsageError
from beherouter.registry import RegistryEntry
from beherouter.reload import (
    Reloader,
    diff,
    drain_s,
    fingerprint,
    watch_s,
    watch_stamp,
    watched_paths,
)


def test_diff_sorts_names_into_four_sets():
    d = diff({"a": "1", "b": "2", "c": "3"}, {"b": "2", "c": "X", "d": "4"})
    assert (d.added, d.removed, d.changed, d.unchanged) == (["d"], ["a"], ["c"], ["b"])


def test_fingerprint_changes_with_a_rotated_file(tmp_path):
    f = tmp_path / "k"
    f.write_text("one")
    e = RegistryEntry(name="s", plugin="p", env={"api_key": f"${{file:{f}}}"})
    before = fingerprint(e)
    f.write_text("two")
    assert fingerprint(e) != before
    assert "one" not in before and "two" not in fingerprint(e)


def test_fingerprint_is_stable_for_an_equal_entry():
    a = RegistryEntry(name="s", plugin="p", config={"x": 1, "y": 2})
    b = RegistryEntry(name="s", plugin="p", config={"y": 2, "x": 1})
    assert fingerprint(a) == fingerprint(b)


def test_watched_paths_include_file_refs(tmp_path):
    reg = {"s": RegistryEntry(name="s", plugin="p", env={"k": "${file:/run/k}", "v": "${V}"})}
    assert watched_paths(tmp_path / "r.toml", reg) == [tmp_path / "r.toml", Path("/run/k")]


def test_watch_stamp_sees_a_change_and_a_missing_file(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text("a")
    s1 = watch_stamp([p])
    os.utime(p, ns=(5, 5))
    assert watch_stamp([p]) != s1
    p.unlink()
    assert watch_stamp([p]) == (None,)


@pytest.mark.parametrize(("raw", "want"), [("", None), ("0", None), ("15", 15.0)])
def test_watch_s(monkeypatch, raw, want):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", raw)
    assert watch_s() == want


@pytest.mark.parametrize("raw", ["-1", "soon", "inf", "nan"])
def test_bad_watch_and_drain_values_are_refused(monkeypatch, raw):
    monkeypatch.setenv("BEHEROUTER_REGISTRY_WATCH_S", raw)
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", raw)
    with pytest.raises(UsageError, match="WATCH_S"):
        watch_s()
    with pytest.raises(UsageError, match="DRAIN_S"):
        drain_s()


async def test_requests_during_a_reload_coalesce_into_one_follow_up():
    gate = asyncio.Event()
    runs: list[str] = []

    async def apply(trigger):
        runs.append(trigger)
        if len(runs) == 1:
            await gate.wait()
        return {"outcome": "ok", "n": len(runs)}

    r = Reloader(apply)
    first = asyncio.create_task(r.request("sighup"))
    await asyncio.sleep(0)
    others = [asyncio.create_task(r.request(t)) for t in ("admin", "watch", "admin")]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(first, *others)
    assert runs == ["sighup", "admin"]  # one follow-up, named by its first requester
    assert results[0]["n"] == 1
    assert all(res["n"] == 2 for res in results[1:])


async def test_an_apply_bug_reaches_every_waiter():
    async def apply(trigger):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await Reloader(apply).request("admin")


async def test_a_failing_first_apply_does_not_strand_the_queued_follow_up():
    gate = asyncio.Event()
    runs: list[str] = []

    async def apply(trigger):
        runs.append(trigger)
        if len(runs) == 1:
            await gate.wait()
            raise RuntimeError("first failed")
        return {"outcome": "ok", "n": len(runs)}

    r = Reloader(apply)
    first = asyncio.create_task(r.request("sighup"))
    await asyncio.sleep(0)
    queued = [asyncio.create_task(r.request(t)) for t in ("admin", "watch")]
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(RuntimeError, match="first failed"):
        await asyncio.wait_for(first, 1)
    results = await asyncio.wait_for(asyncio.gather(*queued), 1)
    assert runs == ["sighup", "admin"]
    assert all(res["n"] == 2 for res in results)


async def test_a_failing_follow_up_reaches_its_waiters():
    gate = asyncio.Event()
    runs: list[str] = []

    async def apply(trigger):
        runs.append(trigger)
        if len(runs) == 1:
            await gate.wait()
            return {"outcome": "ok"}
        raise RuntimeError("follow-up failed")

    r = Reloader(apply)
    first = asyncio.create_task(r.request("sighup"))
    await asyncio.sleep(0)
    queued = asyncio.create_task(r.request("admin"))
    await asyncio.sleep(0)
    gate.set()
    assert (await asyncio.wait_for(first, 1))["outcome"] == "ok"
    with pytest.raises(RuntimeError, match="follow-up failed"):
        await asyncio.wait_for(queued, 1)


async def test_a_cancelled_reload_fails_queued_waiters_instead_of_hanging():
    """Cancelling the RELOAD (shutdown does, through Reloader.cancel) cannot run
    the follow-up: every waiter, the first requester's included, fails."""
    gate = asyncio.Event()

    async def apply(trigger):
        await gate.wait()
        return {}

    r = Reloader(apply)
    first = asyncio.create_task(r.request("sighup"))
    await asyncio.sleep(0)
    queued = asyncio.create_task(r.request("admin"))
    await asyncio.sleep(0)
    await r.cancel()
    assert r.idle
    for waiter in (first, queued):
        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(waiter, 1)


async def test_a_cancelled_requester_cancels_only_its_own_wait():
    """An admin client that disconnects mid-reload must not cut the reload in
    half: the apply completes, and a queued waiter gets the follow-up's result."""
    gate = asyncio.Event()
    runs: list[str] = []
    finished: list[str] = []

    async def apply(trigger):
        runs.append(trigger)
        if len(runs) == 1:
            await gate.wait()
        finished.append(trigger)
        return {"outcome": "ok", "n": len(runs)}

    r = Reloader(apply)
    first = asyncio.create_task(r.request("admin"))
    await asyncio.sleep(0)
    queued = asyncio.create_task(r.request("watch"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert (await asyncio.wait_for(queued, 1))["n"] == 2
    assert finished == ["admin", "watch"]
    assert r.idle


async def test_a_request_after_a_reload_starts_a_new_one():
    runs: list[str] = []

    async def apply(trigger):
        runs.append(trigger)
        return {"n": len(runs)}

    r = Reloader(apply)
    assert (await r.request("admin"))["n"] == 1
    assert (await r.request("watch"))["n"] == 2
    await r.cancel()  # nothing running: a no-op


async def test_a_cancelled_reloader_refuses_later_requests():
    async def apply(trigger):
        return {"outcome": "ok"}

    r = Reloader(apply)
    await r.cancel()
    result = await r.request("sighup")
    assert result["outcome"] == "failed" and "shutting down" in result["error"]
    assert r.idle


async def test_cancel_does_not_swallow_its_callers_own_cancellation():
    gate = asyncio.Event()

    async def apply(trigger):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # a slow unwind
            raise
        return {}

    r = Reloader(apply)
    waiter = asyncio.create_task(r.request("admin"))
    await asyncio.sleep(0)
    closer = asyncio.create_task(r.cancel())
    await asyncio.sleep(0.01)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    with pytest.raises(RuntimeError, match="cancelled"):
        await asyncio.wait_for(waiter, 1)

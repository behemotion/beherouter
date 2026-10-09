"""A10: one token bucket per (surface, caller) (call-gates spec §4)."""

import json
from types import SimpleNamespace

import pytest
from beheaxi import Unavailable
from fastmcp import Client

from beherouter.audit import AuditSink, Caller
from beherouter.gates import SHARED_KEY, Gates, RateLimiter
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.surface import build_surface


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _limiter(calls=60, per_s=60, burst=2):
    clock = Clock()
    return RateLimiter("dwh", calls, per_s, burst, clock=clock), clock


def test_burst_then_refusal_with_a_wait():
    rl, _ = _limiter()
    assert rl.take("a") == 0.0 and rl.take("a") == 0.0
    assert rl.take("a") == 1.0  # 60 per 60 s refills one token a second


def test_refill_is_continuous():
    rl, clock = _limiter()
    rl.take("a"), rl.take("a")
    clock.now = 0.5
    assert rl.take("a") == 0.5
    clock.now = 2.0
    assert rl.take("a") == 0.0


def test_callers_have_separate_buckets():
    rl, _ = _limiter(burst=1)
    assert rl.take("a") == 0.0 and rl.take("b") == 0.0
    assert rl.take("a") > 0


def test_full_buckets_are_evicted_on_the_next_sweep():
    rl, clock = _limiter(calls=1, per_s=1, burst=1)
    rl.take("a")
    clock.now = 1.0
    rl.take("b")
    assert "a" not in rl._buckets and "b" in rl._buckets


def test_from_table_defaults_burst_to_calls():
    rl = RateLimiter.from_table("dwh", {"calls": 30, "per_s": 60})
    assert (rl.calls, rl.per_s, rl.burst, rl.limit) == (30, 60.0, 30, "30/60s")


def _d(name):
    return ToolDescriptor(name=name, verb=name, summary=f"{name} rows", schema={},
                          pinned=True, mutating=False)


async def test_a_limited_call_is_refused_with_retry_after_and_meta_tools_are_free():
    rl = RateLimiter("dwh", calls=1, per_s=60, burst=1)
    lines: list[str] = []
    backend = Backend(name="dwh", kind="mcp", executor=_Ok(), descriptors=[_d("list_tables")])
    surface = build_surface(backend, audit=AuditSink(write=lines.append), gates=Gates(stages=(rl,)))
    async with Client(surface) as c:
        for _ in range(3):
            await c.call_tool("search_tools", {"query": "rows"})
        first = await c.call_tool_mcp("list_tables", {})
        second = await c.call_tool_mcp("list_tables", {})
    assert not first.isError
    meta = second.meta[META_KEY]
    assert meta["reason"] == "rate_limited"
    assert meta["context"] == {"retry_after_s": 60, "limit": "1/60s"}
    assert SHARED_KEY in rl._buckets  # in-process caller: no token -> the shared bucket
    audit = json.loads(lines[-1])
    assert (audit["outcome"], audit["reason"]) == ("refused", "rate_limited")


def _scope(auth, sub=None):
    return SimpleNamespace(caller=Caller(auth=auth, sub=sub))


async def test_oidc_subjects_get_separate_buckets():
    rl, _ = _limiter(burst=1)
    d = _d("list_tables")
    await rl(_scope("oidc", "alice"), d, {})
    with pytest.raises(Unavailable):
        await rl(_scope("oidc", "alice"), d, {})
    await rl(_scope("oidc", "bob"), d, {})  # alice's exhaustion does not touch bob
    assert set(rl._buckets) == {"alice", "bob"}


@pytest.mark.parametrize(
    "auth, sub", [("oidc", None), ("oidc", ""), ("shared", "x"), ("none", None)]
)
async def test_callers_without_a_verified_subject_share_one_bucket(auth, sub):
    rl, _ = _limiter(burst=1)
    await rl(_scope(auth, sub), _d("t"), {})
    assert set(rl._buckets) == {SHARED_KEY}


@pytest.mark.parametrize("wait, expected", [(0.3, 1), (1.2, 2)])
async def test_retry_after_rounds_up_to_whole_seconds(wait, expected):
    # 1 call per 60 s, burst 1: after one take, the wait is 60 s minus the elapsed time.
    rl, clock = _limiter(calls=1, per_s=60, burst=1)
    await rl(_scope("shared"), _d("t"), {})
    clock.now = 60 - wait
    with pytest.raises(Unavailable) as ei:
        await rl(_scope("shared"), _d("t"), {})
    assert ei.value.context["retry_after_s"] == expected


class _Ok:
    async def run(self, verb, args, *, identity=None):
        return {"result": "ok"}

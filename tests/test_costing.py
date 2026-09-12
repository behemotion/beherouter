import json
from pathlib import Path

import pytest

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.costing import (
    CHARS_PER_TOKEN,
    META_TOOL_NAMES,
    METHOD,
    SurfaceCost,
    as_payload,
    estimate_tokens,
    instructions_line,
    naive_tokens,
    surface_cost,
    tool_tokens,
)
from beherouter.models import Backend, ToolDescriptor
from beherouter.surface import build_surface

FIXTURE = Path(__file__).parent / "fixtures/token_calibration.json"


class _Echo:
    async def run(self, verb, args):
        return {"result": verb}


def _d(name, pinned):
    return ToolDescriptor(
        name=name, verb=name, summary=f"Summary for {name}",
        schema={"type": "object", "properties": {"q": {"type": "string"}}},
        pinned=pinned, mutating=False,
    )


def _backend(pinned_count, total):
    ds = [_d(f"tool_{i}", i < pinned_count) for i in range(total)]
    return Backend(name="s", kind="mcp", descriptors=ds, executor=_Echo())


def test_estimator_matches_the_calibration_fixture():
    """The divisor is measured, not guessed. If what beherouter publishes
    changes shape, this fails rather than silently reporting a wrong cost."""
    data = json.loads(FIXTURE.read_text())
    assert abs(data["chars_per_token"] - CHARS_PER_TOKEN) < 0.4, (
        f"calibration drift: fixture says {data['chars_per_token']}, "
        f"costing.py ships {CHARS_PER_TOKEN}. Re-run scripts/calibrate_tokens.py"
    )


def test_estimator_over_counts_rather_than_under_counts():
    """Over-reporting cost is safe; under-reporting invites a host to pin more
    than it can afford. The shipped divisor must not exceed the measured one."""
    data = json.loads(FIXTURE.read_text())
    assert CHARS_PER_TOKEN <= data["chars_per_token"]


def test_estimate_tokens_is_monotonic_and_never_zero_for_content():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)


async def test_tool_tokens_measures_the_published_wire_form():
    surface = build_surface(_backend(1, 1))
    tools = {t.name: t for t in await surface.list_tools()}
    n = tool_tokens(tools["tool_0"])
    wire = tools["tool_0"].to_mcp_tool().model_dump_json(exclude_none=True)
    assert n == estimate_tokens(wire)
    assert n > 0


async def test_surface_cost_splits_meta_from_pinned():
    backend = _backend(3, 10)
    surface = build_surface(backend)
    cost = await surface_cost(surface, backend)
    assert cost.advertised == 10
    assert cost.pinned == 3
    assert cost.tokens_published == cost.tokens_meta + cost.tokens_pinned
    assert cost.tokens_meta > 0
    assert cost.exact is False


async def test_surface_cost_measures_naive_against_the_same_descriptors_as_advertised():
    """`advertised` and `tokens_naive` must come from ONE catalogue.

    context_cost passes the live Catalogue's fresh descriptor list so both
    numbers describe the post-refresh world; before this fix, `advertised`
    moved with the override while `tokens_naive` stayed pinned to
    `backend.descriptors`, so a payload could report a fresh `advertised`
    count beside a `tokens_naive` baseline measured over the stale one.
    """
    backend = _backend(2, 5)
    surface = build_surface(backend)
    baseline = await surface_cost(surface, backend)

    grown = [*backend.descriptors, _d("tool_5", pinned=False)]
    cost = await surface_cost(surface, backend, descriptors=grown)

    assert cost.advertised == 6
    assert cost.tokens_naive > baseline.tokens_naive


async def test_savings_are_positive_for_a_plane_shaped_surface():
    """11 pinned of 30 advertised: the context-savings case."""
    backend = _backend(11, 30)
    cost = await surface_cost(build_surface(backend), backend)
    assert cost.tokens_saved > 0
    assert cost.tokens_naive > cost.tokens_published


async def test_savings_are_negative_for_an_office_shaped_surface():
    """4 tools, all pinned: the gateway costs MORE than a direct connection,
    because the meta-tools are pure overhead.

    AGENTS.md already claims this in prose -- "the benefit here is credential
    centralisation and one uniform client surface, not context savings". This
    is the test that makes the claim fail loudly if it ever stops being true.
    """
    backend = _backend(4, 4)
    cost = await surface_cost(build_surface(backend), backend)
    assert cost.tokens_saved < 0


async def test_naive_tokens_excludes_meta_tools():
    backend = _backend(2, 5)
    naive = await naive_tokens(backend)
    surface = build_surface(backend)
    all_tools = {t.name: t for t in await surface.list_tools()}
    assert not (META_TOOL_NAMES & {"__none__"})
    assert naive == sum(
        tool_tokens(t) for n, t in all_tools.items() if n not in META_TOOL_NAMES
    ) + sum(
        tool_tokens(t)
        for n, t in {
            t.name: t for t in await build_surface(_all_pinned(backend)).list_tools()
        }.items()
        if n not in META_TOOL_NAMES and n not in all_tools
    )


def _all_pinned(backend):
    from dataclasses import replace

    return Backend(
        name=backend.name, kind=backend.kind,
        descriptors=[replace(d, pinned=True) for d in backend.descriptors],
        executor=backend.executor,
    )


async def test_payload_without_a_window_carries_no_percentages():
    backend = _backend(2, 5)
    payload = as_payload(await surface_cost(build_surface(backend), backend))
    assert "pct_published" not in payload
    assert payload["method"].startswith("estimate/json-")
    assert payload["exact"] is False


async def test_payload_with_a_window_carries_percentages():
    """The doc's 1-5% threshold needs a context window beherouter cannot see,
    so the window is an input and the host applies its own threshold."""
    backend = _backend(2, 5)
    cost = await surface_cost(build_surface(backend), backend)
    payload = as_payload(cost, context_window=200_000)
    assert payload["pct_published"] == pytest.approx(
        100 * cost.tokens_published / 200_000, rel=1e-6
    )
    assert payload["pct_naive"] == pytest.approx(
        100 * cost.tokens_naive / 200_000, rel=1e-6
    )


async def test_payload_rejects_a_nonsense_window():
    from beherouter.errors import UsageError

    backend = _backend(2, 5)
    cost = await surface_cost(build_surface(backend), backend)
    with pytest.raises(UsageError):
        as_payload(cost, context_window=0)


async def test_instructions_line_names_the_surface_and_both_figures():
    backend = _backend(11, 30)
    cost = await surface_cost(build_surface(backend), backend)
    line = instructions_line(cost, "plane")
    assert "plane" in line
    assert "11" in line
    assert "30" in line
    assert "search_tools" in line


async def test_cost_of_a_real_cli_surface(fake_cli_cmd):
    """End-to-end against the beheaxi stub, not a synthetic descriptor list."""
    backend = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    cost = await surface_cost(build_surface(backend), backend)
    assert cost.advertised == len(backend.descriptors)
    assert cost.pinned == len(backend.pinned)
    assert cost.tokens_published > cost.tokens_meta


async def test_all_four_readers_report_the_same_number(fake_cli_cmd, tmp_path, monkeypatch, capsys):
    """Divergence here is the exact failure indexing.py was created to prevent.

    The MCP tool, the CLI verb, the health record and the instructions string
    must agree, because they measure one built surface rather than four
    independent reimplementations of what a tool definition costs.
    """
    import asyncio
    import json as _json
    import re

    from fastmcp import Client

    from beherouter.cli.app import app
    from beherouter.gateway import build_surfaces
    from beherouter.health import check_entry
    from beherouter.registry import RegistryEntry

    entry = RegistryEntry(
        name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
    )
    backend = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    surface = build_surface(backend)
    cost = await surface_cost(surface, backend)

    # reader 1: the MCP tool
    async with Client(surface) as c:
        via_tool = (await c.call_tool("context_cost", {})).data

    # reader 2: the health record
    via_health = await check_entry(entry)

    # reader 3: the CLI verb
    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[faketool]\nplugin = "_test-cli"\n\n[faketool.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    # app.main's context-cost calls asyncio.run internally, which refuses to
    # run inside this test's own already-running event loop. Offloading to a
    # thread satisfies asyncio.run's "no running loop in this thread"
    # requirement without the CLI command itself having to hedge against a
    # caller-side situation that never happens outside a test.
    await asyncio.to_thread(app.main, ["context-cost", "--json"])
    via_cli = _json.loads(capsys.readouterr().out)["surfaces"][0]

    # reader 4: the instructions string, as actually shipped. `build_surfaces`
    # is what the gateway calls at attach -- it builds its OWN surface and
    # costs THAT one (gateway.py), so driving this through it rather than
    # reformatting `cost` again is what makes this reader independent of the
    # other three instead of a tautology.
    surfaces = await build_surfaces({"faketool": entry})
    instructions = surfaces["faketool"].instructions
    match = re.search(r"costing about (\d+) tokens", instructions)
    assert match, f"instructions string did not carry a cost figure: {instructions!r}"
    via_instructions = int(match.group(1))

    assert via_tool["tokens_published"] == cost.tokens_published
    assert via_cli["tokens_published"] == cost.tokens_published
    assert via_health["tokens_published"] == cost.tokens_published
    assert via_instructions == cost.tokens_published


def test_instructions_line_does_not_claim_everything_is_published_after_drift():
    """`advertised < pinned` means a PUBLISHED tool vanished from the catalogue.

    The published array is frozen for the process lifetime, so the count of
    published tools cannot fall; the advertised count can. Saying "all N are
    published" there would assert something false about a surface that is in
    fact serving a broken tool.
    """
    cost = SurfaceCost(
        advertised=2,
        pinned=3,
        tokens_meta=100,
        tokens_pinned=50,
        tokens_published=150,
        tokens_naive=40,
        tokens_saved=-110,
        method=METHOD,
    )
    line = instructions_line(cost, "plane")
    assert "All 2 are published" not in line
    assert "no longer" in line

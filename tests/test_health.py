"""Deep (probe-based) backend health.

The motivating incident (2026-07-30): the live `gitea-home` surface answered
tools/list, search_tools and describe_tool perfectly with a REVOKED Gitea PAT,
because all three are served from the attach-time catalogue cached in the
gateway. Only `tools/call` reaches the backend's credential. Every check built on
listing therefore verifies the plumbing and asserts nothing about authorization —
which is how a dead token stayed green past both /healthz and the Prometheus
blackbox probe for a day.

So a real check has to CALL something, and which call is cheap and authenticating
is backend-specific. Hence `probe` on the plugin (overridable per entry), and
these tests.
"""

import pytest
from fastmcp import Client, FastMCP

from beherouter.backends.mcp import backend_from_client
from beherouter.health import check_entry, deep_health, failed
from beherouter.registry import RegistryEntry


@pytest.fixture
async def mcp_loader():
    """A loader over a REAL in-memory MCP backend: one working tool, one that fails
    the way a revoked credential fails."""
    server = FastMCP("fakegitea")

    @server.tool
    def get_me() -> str:
        """Who am I"""
        return "alexandr"

    @server.tool
    def broken() -> str:
        """Fails like a dead PAT"""
        raise RuntimeError("invalid username, password or token")

    @server.tool
    def echo(text: str) -> str:
        """A probe target that REQUIRES an argument"""
        return text

    async with Client(server) as client:

        async def load(entry):
            return await backend_from_client(entry.name, client)

        yield load


def _mcp(name="gitea-home", probe=None, probe_args=None):
    """An entry whose load is faked, so the plugin only has to exist.

    `_test-cli` declares no probe of its own, which keeps these cases about the
    ENTRY's probe; the plugin-default path has its own test below.
    """
    return RegistryEntry(
        name=name,
        plugin="_test-cli",
        config={"cmd": "unused-because-the-loader-is-faked"},
        probe=probe,
        probe_args=probe_args,
    )


async def test_probe_ok_when_the_backend_call_succeeds(mcp_loader):
    record = await check_entry(_mcp(probe="get_me"), load=mcp_loader)
    assert record["attach"] == "ok"
    assert record["probe"] == "ok"


async def test_probe_failed_carries_the_backend_error(mcp_loader):
    """The exact symptom the dead PAT produced must reach the operator verbatim."""
    record = await check_entry(_mcp(probe="broken"), load=mcp_loader)
    assert record["attach"] == "ok"
    assert record["probe"] == "failed"
    assert "invalid username, password or token" in record["error"]


async def test_unconfigured_probe_reports_none_not_ok(mcp_loader):
    """An unprobed backend is UNKNOWN, never green — reporting it as healthy is
    precisely the false confidence this feature exists to remove."""
    record = await check_entry(_mcp(probe=None), load=mcp_loader)
    assert record["attach"] == "ok"
    assert record["probe"] == "none"


async def test_cli_backing_without_a_probe_reports_none(fake_cli_cmd):
    """cli backends execute since 2026-08-04, so an unprobed one is UNKNOWN for
    the same reason an mcp one is — not 'unsupported'."""
    entry = RegistryEntry(name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd})
    record = await check_entry(entry)
    assert record["attach"] == "ok"
    assert record["probe"] == "none"


async def test_cli_backing_probe_actually_runs(fake_cli_cmd):
    """The check that gitea-home did not have, now reaching cli backends too."""
    entry = RegistryEntry(
        name="faketool",
        plugin="_test-cli",
        config={"cmd": fake_cli_cmd},
        probe="search",
        probe_args={"query": "ping"},
    )
    record = await check_entry(entry)
    assert record["probe"] == "ok"


async def test_cli_probe_failure_is_reported(fake_cli_cmd):
    entry = RegistryEntry(
        name="faketool",
        plugin="_test-cli",
        config={"cmd": fake_cli_cmd},
        probe="no-such-verb",
    )
    record = await check_entry(entry)
    assert record["probe"] == "failed"
    assert record["error"]


async def test_probe_args_are_forwarded(mcp_loader):
    """A probe with REQUIRED arguments must still be callable: none of
    office-mcp's four tools take zero args, so a zero-arg-only probe could never
    prove that backend's reachability at all."""
    record = await check_entry(
        _mcp(probe="echo", probe_args={"text": "ping"}), load=mcp_loader
    )
    assert record["probe"] == "ok"


async def test_attach_failure_is_reported_not_raised():
    """One unreachable backend must not abort the whole report."""
    entry = RegistryEntry(
        name="ghost", plugin="_test-cli", config={"cmd": "no-such-binary-xyz"}
    )
    record = await check_entry(entry)
    assert record["attach"] == "failed"
    assert record["probe"] == "skipped"
    assert record["error"]


async def test_deep_health_reports_one_record_per_entry(fake_cli_cmd):
    registry = {
        "faketool": RegistryEntry(
            name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
        ),
        "ghost": RegistryEntry(
            name="ghost", plugin="_test-cli", config={"cmd": "no-such-binary-xyz"}
        ),
    }
    records = await deep_health(registry)
    assert [r["name"] for r in records] == ["faketool", "ghost"]


async def test_empty_registry_is_clean():
    """A gateway fronting nothing is healthy, and has nothing to report."""
    records = await deep_health({})
    assert records == []
    assert failed(records) == []


def test_failed_names_both_attach_and_probe_failures():
    records = [
        {"name": "a", "attach": "ok", "probe": "ok"},
        {"name": "b", "attach": "ok", "probe": "failed"},
        {"name": "c", "attach": "failed", "probe": "skipped"},
        {"name": "d", "attach": "ok", "probe": "none"},
        {"name": "e", "attach": "ok", "probe": "unsupported"},
    ]
    assert failed(records) == ["b", "c"]


async def test_probe_falls_back_to_the_plugin_default(mcp_loader):
    """An entry that omits `probe` gets the plugin's TESTED default, not UNKNOWN.

    This is the gitea-home mistake made unmakeable: forgetting `probe` used to
    yield an unprobed surface that stayed green with a dead credential.
    """
    entry = RegistryEntry(name="office", plugin="office-mcp")
    record = await check_entry(entry, load=mcp_loader)
    assert record["attach"] == "ok"
    # office-mcp's spec probe is `discover`, which the fake backend does not
    # serve — the point is that a probe was ATTEMPTED rather than skipped.
    assert record["probe"] == "failed"


async def test_entry_probe_overrides_the_plugin_default(mcp_loader):
    """Overriding `probe` must not inherit the plugin's `probe_args`.

    They name one tool and its arguments. office-mcp's default is
    discover/{"query": "pdf"}; carrying those args over to `get_me` fails on an
    unexpected keyword and reads exactly like a dead credential.
    """
    entry = RegistryEntry(name="office", plugin="office-mcp", probe="get_me")
    record = await check_entry(entry, load=mcp_loader)
    assert record["probe"] == "ok"


async def test_entry_probe_args_travel_with_the_entry_probe(mcp_loader):
    entry = RegistryEntry(
        name="office", plugin="office-mcp", probe="echo", probe_args={"text": "ping"}
    )
    record = await check_entry(entry, load=mcp_loader)
    assert record["probe"] == "ok"


async def test_a_failing_probe_is_still_probe_failed_after_the_error_split():
    """The 2026-07-30 revoked-PAT scenario is why deep health exists.

    check_entry catches AxiError, the common base of UsageError and
    Unavailable, so narrowing the executor's classification must not let a
    dead credential report green.
    """
    from beherouter.errors import UsageError
    from beherouter.health import PROBE_FAILED, check_entry
    from beherouter.models import Backend
    from beherouter.registry import RegistryEntry

    class _Refusing:
        async def run(self, verb, args):
            raise UsageError("invalid username, password or token")

    async def _load(entry):
        return Backend(name=entry.name, kind="mcp", descriptors=[], executor=_Refusing())

    entry = RegistryEntry(name="gitea", plugin="office-mcp", probe="get_me")
    record = await check_entry(entry, load=_load)
    assert record["probe"] == PROBE_FAILED
    assert "token" in record["error"]


async def test_deep_health_records_carry_cost(fake_cli_cmd):
    """A backend upgrade that grows the catalogue shows up in the monitor."""
    from beherouter.health import check_entry
    from beherouter.registry import RegistryEntry

    entry = RegistryEntry(
        name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
    )
    record = await check_entry(entry)
    assert record["advertised"] > 0
    assert record["tokens_published"] > 0


async def test_a_failed_attach_carries_no_cost_fields():
    """Nothing to measure, so nothing is claimed -- the same stance as
    PROBE_SKIPPED."""
    from beherouter.errors import Unavailable
    from beherouter.health import ATTACH_FAILED, check_entry
    from beherouter.registry import RegistryEntry

    async def _load(entry):
        raise Unavailable("nope")

    record = await check_entry(
        RegistryEntry(name="x", plugin="office-mcp"), load=_load
    )
    assert record["attach"] == ATTACH_FAILED
    assert "tokens_published" not in record


async def test_a_costing_failure_degrades_the_record_not_the_probe(
    fake_cli_cmd, monkeypatch
):
    """A bug in cost computation must not crash the sweep, and must not read as
    PROBE_FAILED -- the same "report, don't take the whole check down" instinct
    as `gateway.build_surfaces`'s sibling guard, and a costing bug must not
    misattribute itself to a dead credential.
    """
    from beherouter import health
    from beherouter.health import ATTACH_OK, PROBE_OK, check_entry
    from beherouter.registry import RegistryEntry

    async def raising(mcp, backend):
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "surface_cost", raising)

    entry = RegistryEntry(
        name="faketool",
        plugin="_test-cli",
        config={"cmd": fake_cli_cmd},
        probe="search",
        probe_args={"query": "ping"},
    )
    record = await check_entry(entry)
    assert record["attach"] == ATTACH_OK
    assert "advertised" not in record
    assert "tokens_published" not in record
    assert record["probe"] == PROBE_OK


async def test_a_pin_check_failure_degrades_the_record_not_the_probe():
    """A bug in the pin-check must not crash the sweep or read as PROBE_FAILED --
    the same degrade-and-log shape as the costing block right above it in
    `health.py`. Attach succeeds (so `entry.pinned`/`plugin.spec.pinned` are
    fine, exactly as AGENTS.md notes), but a malformed descriptor coming back
    from the backend still makes the `d.verb`/`d.name` comparison raise.
    """
    from beherouter.health import ATTACH_OK, PROBE_OK, check_entry
    from beherouter.models import Backend
    from beherouter.registry import RegistryEntry

    class _Ok:
        async def run(self, verb, args):
            return {"result": "ok"}

    async def _load(entry):
        return Backend(
            name=entry.name, kind="mcp", descriptors=[None], executor=_Ok()
        )

    entry = RegistryEntry(name="plane", plugin="plane", pinned=["alive"], probe="alive")
    record = await check_entry(entry, load=_load)
    assert record["attach"] == ATTACH_OK
    assert "catalogue" not in record
    assert "pinned_missing" not in record
    assert record["probe"] == PROBE_OK


async def test_a_served_pin_list_is_catalogue_ok(fake_cli_cmd):
    from beherouter.health import CATALOGUE_OK, check_entry
    from beherouter.registry import RegistryEntry

    entry = RegistryEntry(
        name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
    )
    assert (await check_entry(entry))["catalogue"] == CATALOGUE_OK


async def test_a_vanished_pinned_tool_fails_health():
    """The mechanical version of "re-probe the whole pin list after an upgrade
    or an edition change" -- AGENTS.md's rule, which was manual until now.

    Plane's Community Edition 404s (page, work_log, milestone, workitem_type,
    initiative) are exactly this shape: the catalogue advertises the commercial
    surface, so a tool existing says nothing about this deployment serving it.
    """
    from beherouter.health import CATALOGUE_PINNED_MISSING, check_entry, failed
    from beherouter.models import Backend, ToolDescriptor
    from beherouter.registry import RegistryEntry

    class _Ok:
        async def run(self, verb, args):
            return {"result": "ok"}

    async def _load(entry):
        return Backend(
            name=entry.name, kind="mcp",
            descriptors=[
                ToolDescriptor(
                    name="alive", verb="alive", summary="s", schema={},
                    pinned=True, mutating=None,
                )
            ],
            executor=_Ok(),
        )

    entry = RegistryEntry(
        name="plane", plugin="plane", pinned=["alive", "workitem_type"], probe="alive"
    )
    record = await check_entry(entry, load=_load)
    assert record["catalogue"] == CATALOGUE_PINNED_MISSING
    assert record["pinned_missing"] == ["workitem_type"]
    assert failed([record]) == ["plane"]


async def test_drift_alone_is_not_a_failure(fake_cli_cmd):
    """A backend GAINING tools is normal. Only a broken published tool fails."""
    from beherouter.health import check_entry, failed
    from beherouter.registry import RegistryEntry

    entry = RegistryEntry(
        name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
    )
    assert failed([await check_entry(entry)]) == []


async def test_cli_pinned_verb_is_not_falsely_reported_missing(fake_cli_cmd):
    """`cli` descriptors pin by BARE VERB while their published name is
    FLATTENED (`flatten(surface, verb)`, see `load_cli_backend`). A pin list
    that names the bare verb -- the only shape a `cli` entry's `pinned` can
    take -- must be checked against `d.verb`, never `d.name`: comparing
    against the flat name would report every healthy `cli` surface as
    `pinned_missing`, a fabricated outage on a healthy gateway."""
    from beherouter.health import CATALOGUE_OK, check_entry
    from beherouter.registry import RegistryEntry

    entry = RegistryEntry(
        name="faketool",
        plugin="_test-cli",
        config={"cmd": fake_cli_cmd},
        pinned=["search"],
    )
    record = await check_entry(entry)
    assert record["catalogue"] == CATALOGUE_OK

import httpx
import pytest

from beherouter.errors import UsageError
from beherouter.gateway import load_backend
from beherouter.pluginconfig import render
from beherouter.plugins import get
from beherouter.plugins.calendar.providers.microsoft import MicrosoftCalendar
from beherouter.plugins.m365 import SPEC
from beherouter.registry import RegistryEntry, validate_entry

SIX = {
    "list_calendars",
    "list_events",
    "get_freebusy",
    "create_event",
    "update_event",
    "delete_event",
}


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("BEHE_T_M365_CID", "cid")
    monkeypatch.setenv("BEHE_T_M365_RT", "rt")
    monkeypatch.setenv("BEHE_T_GCAL_CID", "cid")
    monkeypatch.setenv("BEHE_T_GCAL_SEC", "sec")
    monkeypatch.setenv("BEHE_T_GCAL_RT", "rt")


def _m365_entry(**kw):
    body = {
        "name": "m365",
        "plugin": "m365",
        "env": {"client_id": "${BEHE_T_M365_CID}", "refresh_token": "${BEHE_T_M365_RT}"},
    }
    body.update(kw)
    return RegistryEntry(**body)


def _gcal_entry():
    return RegistryEntry(
        name="gcal",
        plugin="gcal",
        env={
            "client_id": "${BEHE_T_GCAL_CID}",
            "client_secret": "${BEHE_T_GCAL_SEC}",
            "refresh_token": "${BEHE_T_GCAL_RT}",
        },
    )


def test_is_registered_as_a_native_plugin():
    assert get("m365").spec is SPEC
    assert SPEC.backing == "native"


def test_declares_no_client_secret():
    """A personal Microsoft account uses a public client: there is no secret to
    hold, and declaring one would make validate_entry demand a credential that
    does not exist."""
    assert {v.name for v in SPEC.env} == {"client_id", "refresh_token"}


def test_probes_with_a_real_credentialed_call():
    assert SPEC.probe == "list_calendars"
    assert not SPEC.probe_args


def test_default_pins_are_all_six_tools():
    assert set(SPEC.pinned) == SIX


def test_requests_offline_access_so_a_refresh_token_is_issued():
    from beherouter.plugins.calendar.providers.microsoft import SCOPE

    assert "offline_access" in SCOPE
    assert "Calendars.ReadWrite" in SCOPE


def test_validate_entry_accepts_a_complete_entry():
    validate_entry(_m365_entry())  # must not raise


def test_validate_entry_rejects_a_client_secret_it_does_not_declare():
    with pytest.raises(UsageError, match="client_secret"):
        validate_entry(
            _m365_entry(
                env={"client_id": "${X}", "refresh_token": "${Y}", "client_secret": "${Z}"}
            )
        )


async def test_build_returns_a_native_backend_wired_to_graph(creds):
    b = await load_backend(_m365_entry())
    assert b.kind == "native"
    assert {d.name for d in b.descriptors} == SIX
    assert isinstance(b.executor._provider, MicrosoftCalendar)


def test_plugin_config_emits_all_three_fragments():
    out = render("m365", "m365")
    assert 'plugin = "m365"' in out["registry"]
    assert "${BEHEROUTER_M365_REFRESH_TOKEN}" in out["registry"]
    assert "^/m365/mcp/?$" in out["caddy"]
    assert "BEHEROUTER_M365_REFRESH_TOKEN={{ vault_beherouter_m365_refresh_token }}" in out["env"]


# --- cross-plugin invariants -------------------------------------------------


async def test_gcal_and_m365_expose_identical_tool_schemas(creds):
    """THE ANTI-DRIFT INVARIANT. The credential, the surface and the Caddy token
    fork; the agent-facing vocabulary must not. An agent's prompt cannot depend
    on which account backs the surface."""
    g = await load_backend(_gcal_entry())
    m = await load_backend(_m365_entry())

    def signature(backend):
        return [
            (d.name, d.verb, d.summary, d.schema, d.pinned, d.mutating)
            for d in backend.descriptors
        ]

    assert signature(g) == signature(m)


def test_both_plugins_declare_the_same_pins():
    from beherouter.plugins.gcal import SPEC as GCAL

    assert GCAL.pinned == SPEC.pinned


async def test_attach_performs_no_network_io(creds, monkeypatch):
    """THE LOAD-BEARING INVARIANT. An attach failure crash-loops the WHOLE
    gateway — /office/mcp, /plane/mcp and /healthz included. A dead refresh
    token must therefore fail the PROBE, which happens on the first run(), not
    at attach. Any socket opened here is a production outage waiting for a
    revoked grant."""

    async def boom(*args, **kwargs):
        raise AssertionError("attach must not perform network I/O")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "request", boom)

    for entry in (_gcal_entry(), _m365_entry()):
        backend = await load_backend(entry)
        assert len(backend.descriptors) == 6


async def test_a_config_table_reaches_the_executor_and_the_provider(creds):
    """m365's calendar_id defaults to None (the mailbox's own calendar), so a
    configured value exercises the other branch of _view_path/_events_path."""
    b = await load_backend(_m365_entry(config={"calendar_id": "AAA", "max_results": 7}))
    assert b.executor._max_results == 7
    assert b.executor._provider._default_calendar == "AAA"


def test_m365_declares_credential_identity_within_its_own_env():
    from beherouter.plugins import get

    spec = get("m365").spec
    assert spec.identity.target == "credential"
    assert spec.identity.modes == ("lookup",)
    declared = {v.name for v in spec.env}
    assert set(spec.identity.accepts) <= declared


async def test_m365_build_provides_a_provider_factory():
    from beherouter.plugins import get
    from beherouter.plugins.spec import PluginContext

    ctx = PluginContext(
        surface="m365",
        config={"calendar_id": "primary", "max_results": 50},
        env={"client_id": "id", "refresh_token": "rt"},
        pinned=list(get("m365").spec.pinned),
    )
    backend = await get("m365").build(ctx)
    assert backend.executor._factory is not None

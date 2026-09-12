import pytest

from beherouter.errors import UsageError
from beherouter.gateway import load_backend
from beherouter.pluginconfig import render
from beherouter.plugins import get
from beherouter.plugins.calendar.providers.google import GoogleCalendar
from beherouter.plugins.gcal import SPEC
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
    monkeypatch.setenv("BEHE_T_GCAL_CID", "cid")
    monkeypatch.setenv("BEHE_T_GCAL_SEC", "sec")
    monkeypatch.setenv("BEHE_T_GCAL_RT", "rt")


def _entry(**kw):
    body = {
        "name": "gcal",
        "plugin": "gcal",
        "env": {
            "client_id": "${BEHE_T_GCAL_CID}",
            "client_secret": "${BEHE_T_GCAL_SEC}",
            "refresh_token": "${BEHE_T_GCAL_RT}",
        },
    }
    body.update(kw)
    return RegistryEntry(**body)


def test_is_registered_as_a_native_plugin():
    assert get("gcal").spec is SPEC
    assert SPEC.backing == "native"


def test_probes_with_a_real_credentialed_call():
    """list_calendars authenticates and takes no arguments — the check
    gitea-home lacked, where a revoked token listed and searched perfectly."""
    assert SPEC.probe == "list_calendars"
    assert not SPEC.probe_args


def test_default_pins_are_all_six_tools():
    """Six tools is small enough that a search tier would be pure overhead."""
    assert set(SPEC.pinned) == SIX


def test_declares_the_three_google_credentials():
    assert {v.name for v in SPEC.env} == {"client_id", "client_secret", "refresh_token"}


def test_requests_the_broad_calendar_scope():
    """Design open question 2: upstream does not document which scope freeBusy
    needs, and a 403 there is indistinguishable from a revoked token. Take the
    broad scope rather than discover the answer in production."""
    from beherouter.plugins.calendar.providers.google import SCOPE

    assert SCOPE == "https://www.googleapis.com/auth/calendar"


def test_validate_entry_accepts_a_complete_entry():
    validate_entry(_entry())  # must not raise


def test_validate_entry_rejects_a_missing_credential():
    with pytest.raises(UsageError, match="refresh_token"):
        validate_entry(_entry(env={"client_id": "${X}", "client_secret": "${Y}"}))


def test_validate_entry_rejects_an_undeclared_credential():
    with pytest.raises(UsageError, match="api_key"):
        validate_entry(
            _entry(
                env={
                    "client_id": "${X}",
                    "client_secret": "${Y}",
                    "refresh_token": "${Z}",
                    "api_key": "${W}",
                }
            )
        )


async def test_an_unset_placeholder_fails_the_load(monkeypatch):
    """An unset ${VAR} raises during build_surfaces, i.e. at startup — which is
    a DEAD GATEWAY, not a broken surface. registry-lint is the pre-deploy guard."""
    monkeypatch.delenv("BEHE_T_GCAL_RT", raising=False)
    monkeypatch.setenv("BEHE_T_GCAL_CID", "cid")
    monkeypatch.setenv("BEHE_T_GCAL_SEC", "sec")
    with pytest.raises(UsageError, match="unset or empty"):
        await load_backend(_entry())


async def test_build_returns_a_native_backend_with_six_tools(creds):
    b = await load_backend(_entry())
    assert b.name == "gcal"
    assert b.kind == "native"
    assert {d.name for d in b.descriptors} == SIX
    assert {d.name for d in b.pinned} == SIX


async def test_build_wires_the_google_adapter(creds):
    b = await load_backend(_entry())
    assert isinstance(b.executor._provider, GoogleCalendar)


async def test_a_pinned_override_is_honoured(creds):
    b = await load_backend(_entry(pinned=["list_calendars", "get_freebusy"]))
    assert {d.name for d in b.pinned} == {"list_calendars", "get_freebusy"}
    assert len(b.descriptors) == 6


def test_plugin_config_emits_all_three_fragments():
    out = render("gcal", "gcal")
    assert 'plugin = "gcal"' in out["registry"]
    assert "${BEHEROUTER_GCAL_REFRESH_TOKEN}" in out["registry"]
    assert "^/gcal/mcp/?$" in out["caddy"]
    assert "BEHEROUTER_GCAL_REFRESH_TOKEN={{ vault_beherouter_gcal_refresh_token }}" in out["env"]


def test_plugin_config_never_emits_a_credential():
    """Like client-config: only a placeholder and the vault variable NAME."""
    env_lines = [ln.strip() for ln in out_registry_env_lines()]
    assert env_lines, "expected an [gcal.env] table"
    for line in env_lines:
        value = line.split("=", 1)[1].strip()
        assert value.startswith('"${BEHEROUTER_GCAL_') and value.endswith('}"'), line


def out_registry_env_lines():
    """The lines of the emitted registry block that live under [gcal.env]."""
    lines = render("gcal", "gcal")["registry"].splitlines()
    start = lines.index("  [gcal.env]")
    return lines[start + 1 :]


async def test_a_config_table_reaches_the_executor_and_the_provider(creds):
    """The registry's [gcal.config] is the only way an operator changes what the
    surface defaults to; nothing else asserts it survives the whole load path."""
    b = await load_backend(
        _entry(config={"calendar_id": "team@example.com", "max_results": 7})
    )
    assert b.executor._max_results == 7
    assert b.executor._provider._default_calendar == "team@example.com"


async def test_config_defaults_apply_when_the_entry_omits_them(creds):
    b = await load_backend(_entry())
    assert b.executor._max_results == 50
    assert b.executor._provider._default_calendar == "primary"

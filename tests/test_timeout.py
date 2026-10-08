import sys
from pathlib import Path

import pytest
from fastmcp import Client

from beherouter import gateway
from beherouter.audit import AuditSink
from beherouter.backends.backing import McpBacking
from beherouter.backends.mcp import ReconnectingMCPExecutor, build_transport
from beherouter.errors import UsageError
from beherouter.gateway import _attach_one, call_timeout_s, default_call_timeout_s, preflight
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.registry import RegistryEntry, load_registry, validate_entry
from beherouter.surface import build_surface

SERVER = Path(__file__).parent / "fixtures" / "generic_mcp.py"


def _sleep_surface(timeout):
    backing = McpBacking(name="slow", transport="stdio", cmd=f"{sys.executable} {SERVER} stdio")
    executor = ReconnectingMCPExecutor(build_transport(backing), backing)
    d = ToolDescriptor(
        name="sleep", verb="sleep", summary="sleep",
        schema={
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
        pinned=True, mutating=False,
    )
    backend = Backend(name="slow", kind="mcp", descriptors=[d], executor=executor)
    return build_surface(backend, call_timeout_s=timeout, audit=AuditSink(enabled=False))


async def test_a_slow_call_times_out_and_the_backend_stays_usable():
    surface = _sleep_surface(0.5)
    async with Client(surface) as c:
        # warm the subprocess so the 0.5 s limit isn't spent on the cold spawn
        await c.call_tool_mcp("sleep", {"seconds": 0})
        slow = await c.call_tool_mcp("sleep", {"seconds": 5})
        fast = await c.call_tool_mcp("sleep", {"seconds": 0})
    assert slow.isError
    assert slow.meta[META_KEY]["reason"] == "timeout"
    assert slow.meta[META_KEY]["context"] == {"limit_s": 0.5}
    assert not fast.isError, fast.content


async def test_no_timeout_by_default():
    surface = _sleep_surface(None)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("sleep", {"seconds": 0.3})
    assert not res.isError


def test_entry_value_wins_over_env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_CALL_TIMEOUT_S", "60")
    assert call_timeout_s(RegistryEntry(name="s", plugin="office-mcp", call_timeout_s=5)) == 5.0
    assert call_timeout_s(RegistryEntry(name="s", plugin="office-mcp")) == 60.0


def test_unset_env_means_no_timeout(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_CALL_TIMEOUT_S", raising=False)
    assert default_call_timeout_s() is None


@pytest.mark.parametrize("raw", ["0", "-1", "soon", "nan", "inf"])
def test_bad_env_is_refused(monkeypatch, raw):
    monkeypatch.setenv("BEHEROUTER_CALL_TIMEOUT_S", raw)
    with pytest.raises(UsageError, match="BEHEROUTER_CALL_TIMEOUT_S"):
        default_call_timeout_s()


@pytest.mark.parametrize("bad", [0, -2, "30", True])
def test_bad_entry_value_is_refused_by_lint(bad):
    entry = RegistryEntry(name="office", plugin="office-mcp", call_timeout_s=bad)
    with pytest.raises(UsageError, match="call_timeout_s"):
        validate_entry(entry)


def test_call_timeout_s_is_read_from_registry_toml(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text('[office]\nplugin = "office-mcp"\ncall_timeout_s = 30\n')
    entry = load_registry(p)["office"]
    assert entry.call_timeout_s == 30
    validate_entry(entry)
    assert call_timeout_s(entry) == 30.0


def _stdio_entry(**kw):
    return RegistryEntry(
        name="notes", plugin="mcp-stdio", pinned=["whoami"], probe="whoami",
        config={"cmd": f"{sys.executable} {SERVER} stdio"}, **kw,
    )


@pytest.mark.parametrize("entry_value, env, expected", [
    (None, "60", 60.0),
    (5, "60", 5.0),
    (None, None, None),
])
async def test_attach_hands_the_resolved_limit_to_build_surface(
    monkeypatch, entry_value, env, expected
):
    if env is None:
        monkeypatch.delenv("BEHEROUTER_CALL_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("BEHEROUTER_CALL_TIMEOUT_S", env)
    seen = {}
    real = gateway.build_surface

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(gateway, "build_surface", spy)
    backends: dict = {}
    entry = _stdio_entry(call_timeout_s=entry_value)
    try:
        await _attach_one("notes", entry, None, None, backends)
    finally:
        for b in backends.values():
            await gateway.close_backend(b)
    assert seen["call_timeout_s"] == expected


def test_preflight_refuses_a_bad_gateway_wide_limit(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_CALL_TIMEOUT_S", "0")
    with pytest.raises(UsageError, match="BEHEROUTER_CALL_TIMEOUT_S"):
        preflight({})

"""The generic plugins — `mcp-http`, `mcp-stdio`, `beheaxi-cli` — against REAL
servers: a FastMCP process on stdio and on HTTP, and beherouter's own CLI as
the beheaxi CLI. Nothing here is mocked below the transport."""

import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from beherouter.cli.app import plugins, registry_lint
from beherouter.errors import UsageError
from beherouter.gateway import load_backend
from beherouter.health import PROBE_OK, check_entry
from beherouter.pluginconfig import render
from beherouter.plugins import get
from beherouter.registry import RegistryEntry, validate_entry

SERVER = Path(__file__).parent / "fixtures" / "generic_mcp.py"
STDIO_CMD = f"{sys.executable} {SERVER} stdio"
# beherouter's own console script IS a beheaxi CLI: a real one, always installed.
BEHEAXI_CMD = str(Path(sys.executable).parent / "beherouter")
PINS = ["search_notes", "whoami"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("generic MCP HTTP fixture did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(timeout=10)


def _http(url, **kw):
    return RegistryEntry(
        name="wiki", plugin="mcp-http", pinned=PINS, probe="whoami",
        config={"url": url, **kw.pop("config", {})}, **kw,
    )


def _stdio(**kw):
    return RegistryEntry(
        name="notes", plugin="mcp-stdio", pinned=PINS, probe="whoami",
        config={"cmd": STDIO_CMD, **kw.pop("config", {})}, **kw,
    )


def _cli(**kw):
    return RegistryEntry(
        name="router", plugin="beheaxi-cli", probe="plugins",
        config={"cmd": BEHEAXI_CMD}, **kw,
    )


# --- the rule the generics exist under: no tested default, so no omission ---


@pytest.mark.parametrize("plugin,config", [
    ("mcp-http", {"url": "http://x:1/mcp"}),
    ("mcp-stdio", {"cmd": "server"}),
])
@pytest.mark.parametrize("missing", ["probe", "pinned"])
def test_generic_mcp_entry_without_probe_or_pinned_is_refused(plugin, config, missing):
    kw = {"probe": "a", "pinned": ["a"]}
    kw.pop(missing)
    with pytest.raises(UsageError, match=f"no tested default for '{missing}'"):
        validate_entry(RegistryEntry(name="s", plugin=plugin, config=config, **kw))


def test_beheaxi_cli_requires_probe_but_not_pinned():
    validate_entry(RegistryEntry(name="s", plugin="beheaxi-cli", probe="x",
                                 config={"cmd": "behesid"}))
    with pytest.raises(UsageError, match="no tested default for 'probe'"):
        validate_entry(RegistryEntry(name="s", plugin="beheaxi-cli",
                                     config={"cmd": "behesid"}))


def test_registry_lint_refuses_a_generic_entry_without_probe(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[wiki]\nplugin = "mcp-http"\npinned = ["a"]\n'
                 '  [wiki.config]\n  url = "http://wiki-mcp:8000/mcp"\n')
    with pytest.raises(UsageError, match="'probe'"):
        registry_lint(path=str(p))


def test_the_credential_is_optional_and_still_closed():
    validate_entry(RegistryEntry(name="s", plugin="mcp-http", probe="a", pinned=["a"],
                                 config={"url": "http://x:1/mcp"}))
    with pytest.raises(UsageError, match="declares no credential"):
        validate_entry(RegistryEntry(name="s", plugin="mcp-http", probe="a", pinned=["a"],
                                     config={"url": "http://x:1/mcp"},
                                     env={"token": "${T}"}))


@pytest.mark.parametrize("url", ["wiki-mcp:8000/mcp", "ftp://x/mcp", "http:///mcp"])
def test_mcp_http_refuses_a_url_that_is_not_http(url):
    with pytest.raises(UsageError, match="http"):
        get("mcp-http").validate({"url": url, "auth_header": "authorization"})


def test_mcp_stdio_refuses_a_bad_variable_name():
    with pytest.raises(UsageError, match="api_key_env"):
        get("mcp-stdio").validate({"cmd": "server", "api_key_env": "MY-KEY"})


def test_stdio_can_never_be_per_user():
    with pytest.raises(UsageError):
        validate_entry(_stdio(identity={"mode": "client", "headers": ["x-api-key"]}))


def test_plugins_marks_the_generics():
    out = {}
    import beherouter.cli.app as appmod

    orig = appmod.app.emit
    appmod.app.emit = out.update
    try:
        plugins()
    finally:
        appmod.app.emit = orig
    generic = {p["name"]: p["generic"] for p in out["plugins"]}
    assert generic["mcp-http"] and generic["mcp-stdio"] and generic["beheaxi-cli"]
    assert not generic["plane"] and not generic["office-mcp"]


# --- plugin-config: three fragments, the optional credential commented out ---


def test_plugin_config_emits_all_three_fragments_for_mcp_http():
    out = render("wiki", "mcp-http")
    block = tomllib.loads(out["registry"].split("\n", 1)[1])["wiki"]
    assert block["plugin"] == "mcp-http"
    assert block["pinned"] == [] and block["probe"] == ""
    assert block["config"] == {"url": ""}
    # Commented out: an uncommented, un-vaulted ${VAR} kills the gateway at boot.
    assert "env" not in block or not block["env"]
    assert "# api_key = \"${BEHEROUTER_WIKI_API_KEY}\"" in out["registry"]
    assert "^/wiki/mcp/?$" in out["caddy"]
    assert out["env"].splitlines()[1].startswith("# BEHEROUTER_WIKI_API_KEY=")


# --- real servers ---


async def test_mcp_http_attaches_probes_and_calls_a_real_server(http_url):
    backend = await load_backend(_http(http_url))
    assert {d.name for d in backend.descriptors if d.pinned} == set(PINS)
    assert "delete_note" in {d.name for d in backend.descriptors}
    out = await backend.executor.run("search_notes", {"query": "x"})
    assert out["result"]["hits"] == ["note about x"]
    record = await check_entry(_http(http_url))
    assert record["probe"] == PROBE_OK and record["catalogue"] == "ok"


async def test_mcp_http_sends_the_credential_in_the_configured_header(http_url, monkeypatch):
    monkeypatch.setenv("WIKI_KEY", "s3cret")
    plain = await load_backend(_http(http_url))
    assert (await plain.executor.run("whoami", {}))["result"]["authorization"] == ""

    bearer = await load_backend(_http(http_url, env={"api_key": "${WIKI_KEY}"}))
    got = (await bearer.executor.run("whoami", {}))["result"]
    assert got["authorization"] == "Bearer s3cret"

    custom = await load_backend(_http(
        http_url, env={"api_key": "${WIKI_KEY}"},
        config={"auth_header": "x-api-key", "auth_prefix": ""},
    ))
    got = (await custom.executor.run("whoami", {}))["result"]
    assert got["x_api_key"] == "s3cret" and got["authorization"] == ""


async def test_mcp_stdio_attaches_probes_and_calls_a_real_server():
    backend = await load_backend(_stdio())
    assert {d.name for d in backend.descriptors if d.pinned} == set(PINS)
    out = await backend.executor.run("search_notes", {"query": "y"})
    assert out["result"]["hits"] == ["note about y"]
    record = await check_entry(_stdio())
    assert record["probe"] == PROBE_OK and record["catalogue"] == "ok"


async def test_mcp_stdio_hands_the_credential_to_the_named_variable(monkeypatch):
    monkeypatch.setenv("NOTES_KEY", "k-123")
    backend = await load_backend(_stdio(
        env={"api_key": "${NOTES_KEY}"}, config={"api_key_env": "GENERIC_MCP_KEY"},
    ))
    assert (await backend.executor.run("whoami", {}))["result"]["env_key"] == "k-123"


@pytest.mark.parametrize("kw", [
    {"env": {"api_key": "${NOTES_KEY}"}},
    {"config": {"api_key_env": "GENERIC_MCP_KEY"}},
])
async def test_mcp_stdio_refuses_half_a_credential_pair(kw, monkeypatch):
    monkeypatch.setenv("NOTES_KEY", "k-123")
    with pytest.raises(UsageError, match="api_key"):
        await load_backend(_stdio(**kw))


async def test_mcp_stdio_missing_command_is_refused_by_name():
    entry = _stdio(config={"cmd": "no-such-mcp-server --stdio"})
    with pytest.raises(UsageError, match="no-such-mcp-server"):
        await load_backend(entry)


async def test_beheaxi_cli_attaches_a_real_beheaxi_cli_and_probes_it():
    backend = await load_backend(_cli())
    assert backend.kind == "cli"
    verbs = {d.verb for d in backend.descriptors}
    assert {"plugins", "registry-lint", "describe"} & verbs
    # Published names are flattened `<surface>_<verb>`; pins come from the manifest.
    assert all(d.name.startswith("router_") for d in backend.descriptors)
    assert any(d.pinned for d in backend.descriptors)
    out = await backend.executor.run("plugins", {})
    assert "mcp-http" in {p["name"] for p in out["plugins"]}
    record = await check_entry(_cli())
    assert record["probe"] == PROBE_OK and record["catalogue"] == "ok"


async def test_beheaxi_cli_pins_override_the_manifest_by_bare_verb():
    backend = await load_backend(_cli(pinned=["plugins"]))
    assert {d.verb for d in backend.descriptors if d.pinned} == {"plugins"}


def test_registry_lint_warns_when_a_cli_command_is_not_installed(tmp_path, capsys):
    p = tmp_path / "r.toml"
    p.write_text('[sid]\nplugin = "beheaxi-cli"\nprobe = "inspect"\n'
                 '  [sid.config]\n  cmd = "no-such-beheaxi-cli"\n')
    registry_lint(path=str(p))
    assert "no-such-beheaxi-cli" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("plugin", "config", "match"),
    [
        ("beheaxi-cli", {"cmd": "  "}, "beheaxi-cli: cmd must not be empty"),
        ("mcp-stdio", {"cmd": ""}, "mcp-stdio: cmd must not be empty"),
        ("mcp-http", {"url": "http://h/mcp", "auth_header": ""},
         "mcp-http: auth_header must not be empty"),
    ],
)
def test_an_empty_required_value_is_refused(plugin, config, match):
    with pytest.raises(UsageError, match=match):
        get(plugin).validate(config)

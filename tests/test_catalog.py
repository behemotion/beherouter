"""catalog-import / catalog-export: server.json in, registry fragments out.

The fixtures under tests/fixtures/server_json/ are real registry replies where
they can be (see that directory's README), so the importer is held to what
publishers actually write rather than to the schema's best case.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from beherouter.catalog import (
    PLUGIN_KEY,
    PUBLISHER_KEY,
    SCHEMA_VERSION,
    _toml,
    export_plugin,
    import_server,
    load_source,
)
from beherouter.errors import NotFound, Unavailable, UsageError
from beherouter.plugins import PLUGINS
from beherouter.registry import load_registry, validate_entry

FIXTURES = Path(__file__).parent / "fixtures" / "server_json"


def _load(name: str) -> dict:
    server, _ = load_source(str(FIXTURES / name))
    return server


def _entry_toml(result: dict) -> dict:
    return tomllib.loads(result["registry"])


def _lint(tmp_path: Path, registry_text: str):
    path = tmp_path / "registry.toml"
    path.write_text(registry_text)
    for entry in load_registry(path).values():
        validate_entry(entry)


# --- each shape -------------------------------------------------------------


def test_an_npm_stdio_package_becomes_mcp_stdio_pinned_to_its_exact_version():
    result = import_server(_load("brave-npm.json"), "brave")
    body = _entry_toml(result)["brave"]
    assert result["plugin"] == body["plugin"] == "mcp-stdio"
    assert body["config"]["cmd"] == "npx -y @brave/brave-search-mcp-server@2.1.3"
    assert body["config"]["api_key_env"] == "BRAVE_API_KEY"
    assert body["env"] == {"api_key": "${BEHEROUTER_BRAVE_API_KEY}"}
    assert "BEHEROUTER_BRAVE_API_KEY=" in result["env"]
    assert any("must ship `npx`" in w for w in result["warnings"])


def test_a_pypi_stdio_package_uses_uvx_with_an_exact_pin():
    result = import_server(_load("serena-pypi.json"), "serena")
    cmd = _entry_toml(result)["serena"]["config"]["cmd"]
    assert cmd.startswith("uvx ") and "serena-agent==1.5.3" in cmd
    assert any("must ship `uvx`" in w for w in result["warnings"])
    # serena puts its server arguments under runtimeArguments; say so.
    assert any("runtime arguments" in w for w in result["warnings"])


def test_a_streamable_http_remote_becomes_mcp_http_and_wins_over_a_package():
    result = import_server(_load("github-remote-oci.json"), "github")
    body = _entry_toml(result)["github"]
    assert body["plugin"] == "mcp-http"
    assert body["config"] == {
        "url": "https://api.githubcopilot.com/mcp/",
        "auth_header": "authorization",
        "auth_prefix": "Bearer ",  # an Authorization header with no template
    }
    # The secret is optional in this server.json, so its line is commented out:
    # an uncommented ${VAR} nobody vaulted is a dead gateway at startup.
    assert "env" not in body or not body["env"]
    assert "# api_key = " in result["registry"]
    assert any("oci" in w and "not imported" in w for w in result["warnings"])


def test_a_header_template_sets_header_and_prefix():
    server = {
        "name": "io.example/x", "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.example/mcp", "headers": [
            {"name": "Authorization", "value": "Token {key}", "isSecret": True,
             "isRequired": True},
        ]}],
    }
    config = _entry_toml(import_server(server, "x"))["x"]["config"]
    assert config["auth_header"] == "authorization" and config["auth_prefix"] == "Token "


def test_an_sse_remote_is_named_not_imported():
    result = import_server(_load("notion-remote.json"), "notion")
    assert _entry_toml(result)["notion"]["config"] == {"url": "https://mcp.notion.com/mcp"}
    assert any("sse" in w and "not imported" in w for w in result["warnings"])
    # Recorded against an older schema; the importer says so instead of guessing.
    assert any("2025-09-29" in w for w in result["warnings"])


def test_nothing_importable_is_refused_by_name():
    server = {"name": "io.example/oci-only", "version": "1",
              "packages": [{"registryType": "oci", "identifier": "x", "version": "1",
                            "transport": {"type": "stdio"}}]}
    with pytest.raises(UsageError, match="nothing here can be attached"):
        import_server(server, "x")


@pytest.mark.parametrize("version", ["latest", "", "^1.2.0", ">=1.0"])
def test_a_package_without_an_exact_version_is_refused(version):
    server = {"name": "io.example/x", "version": "1",
              "packages": [{"registryType": "npm", "identifier": "x", "version": version,
                            "transport": {"type": "stdio"}}]}
    with pytest.raises(UsageError, match="exact version"):
        import_server(server, "x")


def test_a_bad_surface_name_is_refused():
    with pytest.raises(UsageError, match="lowercase"):
        import_server(_load("brave-npm.json"), "Brave_Search")


# --- secrets: one carried, the rest named ---------------------------------


def test_more_than_one_secret_carries_one_and_warns_for_the_rest():
    result = import_server(_load("multi-secret-pypi.json"), "crm")
    body = _entry_toml(result)["crm"]
    assert body["config"]["api_key_env"] == "CRM_TOKEN"  # the REQUIRED one
    assert body["env"] == {"api_key": "${BEHEROUTER_CRM_API_KEY}"}
    dropped = [w for w in result["warnings"] if "CRM_SIGNING_KEY" in w]
    assert len(dropped) == 1 and "cannot carry" in dropped[0]
    # A non-secret with a default rides on the command line.
    assert body["config"]["cmd"].startswith("env CRM_URL=https://crm.example.com uvx ")


def test_a_second_secret_header_is_named_too():
    server = {
        "name": "io.example/x", "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.example/mcp", "headers": [
            {"name": "X-Api-Key", "isSecret": True, "isRequired": True},
            {"name": "X-Tenant-Secret", "isSecret": True, "isRequired": True},
            {"name": "X-Region", "value": "eu"},
        ]}],
    }
    warnings = import_server(server, "x")["warnings"]
    assert any("X-Tenant-Secret" in w and "cannot carry" in w for w in warnings)
    assert any("X-Region" in w and "not carried" in w for w in warnings)


def test_a_secret_in_argv_is_refused_with_a_warning_not_dropped_silently():
    server = {"name": "io.example/x", "version": "1", "packages": [{
        "registryType": "npm", "identifier": "x", "version": "1.0.0",
        "transport": {"type": "stdio"},
        "packageArguments": [{"type": "named", "name": "--token", "value": "{t}",
                              "variables": {"t": {"isSecret": True}}}],
    }]}
    result = import_server(server, "x")
    assert "--token" not in _entry_toml(result)["x"]["config"]["cmd"]
    assert any("--token" in w and "SECRET in argv" in w for w in result["warnings"])


# --- the lint gate ---------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", ["brave-npm.json", "serena-pypi.json", "github-remote-oci.json",
                "notion-remote.json", "multi-secret-pypi.json"],
)
def test_an_import_without_the_meta_block_fails_lint_until_the_todos_are_replaced(
    tmp_path, fixture
):
    result = import_server(_load(fixture), "s")
    with pytest.raises(UsageError, match=r"no tested default for 'probe'|'pinned'"):
        _lint(tmp_path, result["registry"])
    chosen = (
        result["registry"]
        .replace('pinned = []', 'pinned = ["a_tool"]')
        .replace('probe = ""', 'probe = "a_tool"')
    )
    _lint(tmp_path, chosen)


def test_the_meta_block_fills_pins_probe_and_aliases_and_passes_lint(tmp_path):
    result = import_server(_load("wiki-remote-meta.json"), "wiki")
    body = _entry_toml(result)["wiki"]
    assert body["pinned"] == ["search_pages", "get_page"]
    assert body["probe"] == "search_pages"
    assert body["probe_args"] == {"query": "a", "limit": 1}
    assert body["search_aliases"] == {"search_pages": ["wiki", "docs"]}
    assert body["config"]["auth_header"] == "x-api-key" and body["config"]["auth_prefix"] == ""
    assert "TODO" not in result["registry"]
    _lint(tmp_path, result["registry"])


def test_meta_identity_is_honoured_only_where_the_plugin_declares_it():
    result = import_server(_load("wiki-remote-meta.json"), "wiki")
    body = _entry_toml(result)["wiki"]
    assert "identity" not in body  # suggested, commented out, never enabled
    assert '# mode = "bearer"' in result["registry"]
    assert any("['lookup']" in w and "does not honour" in w for w in result["warnings"])


def test_a_malformed_meta_key_degrades_to_a_todo_with_a_warning():
    server = _load("wiki-remote-meta.json")
    server["_meta"][PUBLISHER_KEY][PLUGIN_KEY]["pinned"] = "search_pages"
    result = import_server(server, "wiki")
    assert 'pinned = []    # TODO' in result["registry"]
    assert any("`pinned` is malformed" in w for w in result["warnings"])


def test_an_unknown_meta_version_is_ignored_with_a_warning():
    server = _load("wiki-remote-meta.json")
    server["_meta"][PUBLISHER_KEY][PLUGIN_KEY]["v"] = 2
    result = import_server(server, "wiki")
    assert "TODO" in result["registry"]
    assert any("version 2" in w for w in result["warnings"])


def test_registry_lint_passes_an_imported_meta_entry_and_warns_it_is_declared(tmp_path):
    result = import_server(_load("wiki-remote-meta.json"), "wiki")
    (tmp_path / "registry.toml").write_text(result["registry"])
    r = subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", "registry-lint", "--json"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "BEHEROUTER_REGISTRY": str(tmp_path / "registry.toml"),
             "BEHEROUTER_WIKI_API_KEY": "x"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    warnings = json.loads(r.stdout)["warnings"]
    assert any("'wiki': plugin 'mcp-http' is maturity 'declared'" in w for w in warnings)


# --- sources: file, URL, registry name -------------------------------------


def _mock(handler):
    return httpx.MockTransport(handler)


def test_a_url_source_is_fetched_and_the_api_wrapper_unwrapped():
    payload = json.loads((FIXTURES / "brave-npm.json").read_text())

    def handler(request):
        assert str(request.url) == "https://example.test/server.json"
        return httpx.Response(200, json=payload)

    server, origin = load_source("https://example.test/server.json", transport=_mock(handler))
    assert server["name"] == "io.github.brave/brave-search-mcp-server"
    assert origin == "https://example.test/server.json"


def test_a_registry_name_is_looked_up_latest_by_default():
    payload = json.loads((FIXTURES / "brave-npm.json").read_text())
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=payload)

    server, origin = load_source(
        "io.github.brave/brave-search-mcp-server",
        registry="https://registry.example.test/",
        transport=_mock(handler),
    )
    assert seen == ["/v0/servers/io.github.brave%2Fbrave-search-mcp-server/versions/latest"]
    assert server["version"] == "2.1.3"
    assert "registry.example.test" in origin


def test_a_registry_name_may_pin_a_version():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"server": {"name": "io.example/x", "version": "1.0.0"}})

    load_source("io.example/x@1.0.0", registry="https://r.test", transport=_mock(handler))
    assert seen == ["/v0/servers/io.example%2Fx/versions/1.0.0"]


def test_an_unknown_registry_name_is_not_found():
    with pytest.raises(NotFound, match=r"no server 'io.example/missing'"):
        load_source("io.example/missing", registry="https://r.test",
                    transport=_mock(lambda r: httpx.Response(404)))


def test_an_unreachable_source_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(Unavailable):
        load_source("https://example.test/s.json", transport=_mock(handler))


def test_something_that_is_none_of_the_three_is_not_found(tmp_path):
    with pytest.raises(NotFound, match="not a file, an http"):
        load_source(str(tmp_path / "missing.json"))


def test_a_json_file_that_is_not_a_server_is_refused(tmp_path):
    (tmp_path / "x.json").write_text("[]")
    with pytest.raises(UsageError, match=r"not a server.json"):
        load_source(str(tmp_path / "x.json"))


# --- export and the round trip ---------------------------------------------


@pytest.mark.parametrize(
    "plugin,extra",
    [("office-mcp", {}), ("sonarqube", {}),
     ("plane", {"package": "pypi:plane-mcp-server@0.3.2"})],
)
def test_export_then_import_round_trips_pins_probe_and_aliases(plugin, extra):
    spec = PLUGINS[plugin].spec
    exported = export_plugin(plugin, **extra)
    server = json.loads(json.dumps(exported["server"]))  # what a file would hold
    body = _entry_toml(import_server(server, "s"))["s"]
    assert body["pinned"] == list(spec.pinned)
    assert body["probe"] == spec.probe
    assert body["probe_args"] == spec.probe_args
    assert body["search_aliases"] == {k: list(v) for k, v in spec.search_aliases.items()}


def test_export_writes_the_meta_block_under_the_publisher_key():
    server = export_plugin("plane-http", url="https://plane-mcp.example.com/bearer/mcp")["server"]
    assert server["$schema"].endswith("/2025-12-11/server.schema.json")
    assert server["name"] == "io.beherouter/plane-http"
    assert len(server["description"]) <= 100
    block = server["_meta"][PUBLISHER_KEY][PLUGIN_KEY]
    assert block["v"] == 1
    identity = PLUGINS["plane-http"].spec.identity
    assert block["identity"] == {"modes": list(identity.modes), "target": identity.target}


def test_export_says_what_server_json_cannot_carry():
    warnings = export_plugin("sonarqube")["warnings"]
    assert any("['api_key'] are not described" in w for w in warnings)
    assert any("in-network default" in w for w in warnings)


def test_export_refuses_what_has_nothing_to_export():
    with pytest.raises(UsageError, match="generic"):
        export_plugin("mcp-http")
    with pytest.raises(UsageError, match="runs inside the gateway"):
        export_plugin("gcal")
    with pytest.raises(UsageError, match="--package"):
        export_plugin("plane")
    with pytest.raises(UsageError, match="--package must be"):
        export_plugin("plane", package="plane-mcp-server")
    with pytest.raises(UsageError, match="--url"):
        export_plugin("plane-http")


# --- the CLI ----------------------------------------------------------------


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", *args],
        capture_output=True, text=True, check=False,
    )


def test_cli_catalog_import_emits_json():
    r = _run("--json", "catalog-import", str(FIXTURES / "brave-npm.json"), "brave")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["plugin"] == "mcp-stdio"


def test_cli_catalog_import_of_a_missing_file_exits_not_found():
    r = _run("--json", "catalog-import", "/nonexistent/server.json", "x")
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)


def test_cli_catalog_export_emits_the_server():
    r = _run("--json", "catalog-export", "office-mcp", "--server-version", "0.1.0")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["server"]["version"] == "0.1.0"


def test_cli_catalog_verbs_are_listed_unpinned_and_read_only():
    verbs = {v["name"]: v for v in json.loads(_run("describe", "--json").stdout)["verbs"]}
    for name in ("catalog-import", "catalog-export"):
        assert verbs[name]["pinned"] is False and verbs[name]["mutating"] is False


def test_cli_plugins_carries_the_tier():
    r = _run("--json", "plugins")
    rows = {p["name"]: p for p in json.loads(r.stdout)["plugins"]}
    assert rows["plane-http"]["maturity"] == "per-user"
    assert rows["mcp-http"]["maturity"] == "declared"


def test_a_file_that_is_not_utf8_is_refused_as_not_json(tmp_path):
    """A UnicodeDecodeError is a ValueError without `.msg`; it used to escape
    as an AttributeError instead of the UsageError every other bad file gets."""
    (tmp_path / "x.json").write_bytes(b"\xff\xfe\x00{")
    with pytest.raises(UsageError, match="not JSON"):
        load_source(str(tmp_path / "x.json"))


# --- edges of the source reader --------------------------------------------


def test_an_http_error_other_than_404_is_unavailable():
    with pytest.raises(Unavailable, match="HTTP 502"):
        load_source("https://example.test/s.json",
                    transport=_mock(lambda r: httpx.Response(502)))


def test_a_url_that_answers_non_json_is_refused():
    with pytest.raises(UsageError, match="did not return JSON"):
        load_source("https://example.test/s.json",
                    transport=_mock(lambda r: httpx.Response(200, text="<html>")))


# --- edges of import -------------------------------------------------------


def _server(**fields) -> dict:
    return {"$schema": f"https://x/{SCHEMA_VERSION}/server.schema.json",
            "name": "io.example/x", "version": "1.0.0", **fields}


def _npm(**fields) -> dict:
    return {"registryType": "npm", "identifier": "@ex/x", "version": "1.0.0",
            "transport": {"type": "stdio"}, **fields}


def _meta(block) -> dict:
    return {"_meta": {PUBLISHER_KEY: {PLUGIN_KEY: block}}}


def test_toml_renders_booleans_and_refuses_what_it_cannot_render():
    assert _toml([True, False]) == "[true, false]"
    with pytest.raises(UsageError, match="cannot render NoneType"):
        _toml(None)


def test_a_meta_block_that_is_not_an_object_is_ignored_with_a_warning():
    result = import_server(_server(packages=[_npm()], **_meta(["x"])), "x")
    assert any("is not an object" in w for w in result["warnings"])
    assert "TODO: choose the pinned tools" in result["registry"]


def test_meta_probe_args_without_a_probe_is_dropped():
    block = {"v": 1, "pinned": ["a"], "probe_args": {"q": 1}}
    result = import_server(_server(packages=[_npm()], **_meta(block)), "x")
    assert any("`probe_args` without `probe`" in w for w in result["warnings"])
    assert "probe_args" not in _entry_toml(result)["x"]


def test_a_meta_probe_without_args_emits_no_probe_args():
    block = {"v": 1, "pinned": ["a"], "probe": "a"}
    body = _entry_toml(import_server(_server(packages=[_npm()], **_meta(block)), "x"))["x"]
    assert body["probe"] == "a" and "probe_args" not in body


def test_meta_identity_wholly_unhonoured_emits_no_identity_block():
    block = {"v": 1, "identity": {"modes": ["bearer"]}}
    result = import_server(_server(packages=[_npm()], **_meta(block)), "x")
    assert any("can never be per-user" in w for w in result["warnings"])
    assert "identity]" not in result["registry"]


def test_each_package_not_chosen_says_why():
    remote = {"type": "streamable-http", "url": "https://x.test/mcp"}
    w = import_server(_server(remotes=[remote], packages=[_npm()]), "x")["warnings"]
    assert any("the streamable-http remote was preferred" in x for x in w)
    second = _npm(identifier="@ex/y")
    w = import_server(_server(packages=[_npm(), second]), "x")["warnings"]
    assert any("@ex/y not imported: only one source becomes the entry" in x for x in w)


def test_url_variables_fill_from_defaults_and_warn_without_one():
    remote = {"type": "streamable-http", "url": "https://{host}/{tenant}/mcp",
              "variables": {"host": {"default": "x.test"}, "tenant": {}}}
    result = import_server(_server(remotes=[remote]), "x")
    assert _entry_toml(result)["x"]["config"]["url"] == "https://x.test/{tenant}/mcp"
    assert any("'{tenant}' has no default" in w for w in result["warnings"])


def test_a_secret_header_with_text_after_its_placeholder_warns():
    header = {"name": "X-Key", "isSecret": True, "value": "Key {k} suffix"}
    remote = {"type": "streamable-http", "url": "https://x.test/mcp", "headers": [header]}
    result = import_server(_server(remotes=[remote]), "x")
    assert _entry_toml(result)["x"]["config"]["auth_prefix"] == "Key "
    assert any("text after its placeholder" in w for w in result["warnings"])


def test_a_secret_header_with_a_fixed_value_warns_to_check_the_prefix():
    header = {"name": "X-Key", "isSecret": True, "value": "fixed"}
    remote = {"type": "streamable-http", "url": "https://x.test/mcp", "headers": [header]}
    result = import_server(_server(remotes=[remote]), "x")
    assert _entry_toml(result)["x"]["config"]["auth_prefix"] == ""
    assert any("fixed value and no placeholder" in w for w in result["warnings"])


def test_package_argument_shapes():
    args = [
        {"type": "named", "name": "--port", "value": "{port}",
         "variables": {"port": {"default": 8080}}},
        {"type": "named", "name": "--verbose"},
        {"type": "positional", "valueHint": "dir"},
    ]
    result = import_server(_server(packages=[_npm(packageArguments=args)]), "x")
    cmd = _entry_toml(result)["x"]["config"]["cmd"]
    assert cmd.endswith("@ex/x@1.0.0 --port 8080 --verbose")
    assert any("argument 'dir' has no value or default" in w for w in result["warnings"])


def test_package_runtime_hint_index_and_valueless_env_are_named():
    pkg = _npm(runtimeHint="bunx", registryBaseUrl="https://npm.corp.test",
               environmentVariables=[{"name": "REGION", "isRequired": True}])
    w = import_server(_server(packages=[pkg]), "x")["warnings"]
    assert any("runtimeHint is 'bunx'" in x for x in w)
    assert any("comes from https://npm.corp.test" in x for x in w)
    assert any("'REGION' is required and has no value" in x for x in w)


def test_an_optional_stdio_secret_keeps_api_key_env_commented_with_it():
    pkg = _npm(environmentVariables=[{"name": "TOKEN", "isSecret": True}])
    result = import_server(_server(packages=[pkg]), "x")
    assert "  # api_key_env = \"TOKEN\"    # uncomment with api_key" in result["registry"]
    assert "  # api_key = " in result["registry"]
    body = _entry_toml(result)["x"]
    assert "api_key_env" not in body["config"] and not body.get("env")


# --- edges of export -------------------------------------------------------


def test_export_refuses_a_name_that_is_not_namespace_slash_name():
    with pytest.raises(UsageError, match="--name must be"):
        export_plugin("office-mcp", name="no-slash")


def test_export_version_falls_back_when_beherouter_is_not_installed(monkeypatch):
    import importlib.metadata as md

    def missing(_name):
        raise md.PackageNotFoundError(_name)

    monkeypatch.setattr(md, "version", missing)
    exported = export_plugin("office-mcp")
    assert exported["server"]["version"] == "0.0.0"


def test_export_of_a_spec_without_probe_or_aliases_omits_them(monkeypatch):
    import dataclasses
    from types import SimpleNamespace

    import beherouter.plugins as plugins

    bare = dataclasses.replace(PLUGINS["office-mcp"].spec, probe=None, probe_args=None,
                               search_aliases={})
    with_probe = dataclasses.replace(bare, probe="discover")
    for spec in (bare, with_probe):
        monkeypatch.setattr(plugins, "get", lambda _n, spec=spec: SimpleNamespace(spec=spec))
        block = export_plugin("office-mcp", version="1.0.0")["server"]["_meta"][
            PUBLISHER_KEY][PLUGIN_KEY]
        assert "probe_args" not in block and "search_aliases" not in block
        assert block.get("probe") == spec.probe

import json
import os
import subprocess
import sys


def _run(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _env(tmp_path, **extra):
    # BEHEROUTER_TEST_PLUGINS reaches the SUBPROCESS: conftest sets it for this
    # interpreter, but `python -m beherouter.cli.app` never imports conftest, so
    # without it the `_test-cli` plugin these tests attach does not exist there.
    return {
        **os.environ,
        "BEHEROUTER_TEST_PLUGINS": "1",
        "BEHEROUTER_REGISTRY": str(tmp_path / "registry.toml"),
        **extra,
    }


def test_describe_json_conforms():
    r = _run(["describe", "--json"])
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["tool"] == "beherouter"
    names = {v["name"] for v in m["verbs"]}
    assert {"surfaces", "attach", "detach", "health", "search"} <= names


def test_describe_output_validates_against_beheaxi_schema():
    """Dogfood: our own manifest must pass the validator we impose on siblings."""
    from beherouter.manifest import validate_manifest

    r = _run(["describe", "--json"])
    validate_manifest(json.loads(r.stdout))


def test_pinned_verbs_are_the_operator_facing_set():
    m = json.loads(_run(["describe", "--json"]).stdout)
    pinned = {v["name"] for v in m["verbs"] if v["pinned"]}
    assert pinned == {
        "surfaces",
        "health",
        "search",
        "context-cost",
        "client-config",
        "plugins",
        "plugin-config",
        "registry-lint",
    }


def test_attach_then_surfaces(tmp_path, fake_cli_cmd):
    env = _env(tmp_path)
    a = _run(
        ["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"], env=env
    )
    assert a.returncode == 0, a.stderr
    s = _run(["surfaces", "--json"], env=env)
    assert "faketool" in s.stdout


def test_attach_bad_cmd_exits_unavailable(tmp_path):
    """Exit codes are the CLI contract: Unavailable == 6."""
    env = _env(tmp_path)
    r = _run(
        ["attach", "nope", "_test-cli", "--config", "cmd=no-such-binary-xyz"], env=env
    )
    assert r.returncode == 6


def test_attach_unknown_plugin_exits_usage(tmp_path):
    env = _env(tmp_path)
    r = _run(["attach", "x", "no-such-plugin"], env=env)
    assert r.returncode == 2


def test_detach_missing_exits_not_found(tmp_path):
    env = _env(tmp_path)
    r = _run(["detach", "ghost"], env=env)
    assert r.returncode == 3


def test_attach_then_detach_roundtrip(tmp_path, fake_cli_cmd):
    env = _env(tmp_path)
    _run(
        ["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"], env=env
    )
    d = _run(["detach", "faketool"], env=env)
    assert d.returncode == 0, d.stderr
    s = _run(["surfaces", "--json"], env=env)
    assert "faketool" not in s.stdout


def test_detach_leaves_another_surfaces_identity_map_intact(tmp_path):
    """`detach` rewrites the whole file. It used to write every OTHER surface's
    `[x.identity.map]` back as a repr string — a per-user surface silently
    broken by an operator removing an unrelated one."""
    from beherouter.registry import load_registry

    reg = tmp_path / "registry.toml"
    reg.write_text(
        '[wiki]\nplugin = "office-mcp"\n\n[wiki.identity]\nmode = "claims"\n\n'
        '[wiki.identity.map]\n"x-remote-user" = "email"\n\n'
        '[gone]\nplugin = "office-mcp"\n'
    )
    r = _run(["detach", "gone"], env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert load_registry(reg)["wiki"].identity == {
        "mode": "claims",
        "map": {"x-remote-user": "email"},
    }


def test_search_finds_long_tail(tmp_path, fake_cli_cmd):
    env = _env(tmp_path)
    _run(
        ["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"], env=env
    )
    r = _run(["search", "faketool", "create shelf", "--json"], env=env)
    assert r.returncode == 0, r.stderr
    assert "faketool_shelf_create" in r.stdout


def test_search_unknown_tool_exits_not_found(tmp_path):
    env = _env(tmp_path)
    r = _run(["search", "ghost", "anything"], env=env)
    assert r.returncode == 3


def test_health_reports_counts(tmp_path, fake_cli_cmd):
    env = _env(tmp_path)
    _run(
        ["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"], env=env
    )
    r = _run(["health", "--json"], env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["count"] == 1


# --- health --deep ----------------------------------------------------------
#
# Shallow health cannot see a revoked credential: list/search/describe are all
# answered from the attach-time catalogue. `--deep` calls each backend's
# configured probe, and the exit code carries the verdict so a monitoring
# wrapper needs nothing but `$?`.


def _registry(tmp_path, body):
    (tmp_path / "registry.toml").write_text(body)
    return _env(tmp_path)


DEAD_BACKEND = (
    '[ghost]\nplugin = "_test-cli"\n'
    '  [ghost.config]\n  cmd = "no-such-binary-xyz"\n'
)


def test_health_deep_reports_a_record_per_backend(tmp_path, fake_cli_cmd):
    env = _env(tmp_path)
    _run(
        ["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"], env=env
    )
    r = _run(["health", "--deep", "--json"], env=env)
    assert r.returncode == 0, r.stderr
    backends = json.loads(r.stdout)["backends"]
    assert len(backends) == 1
    record = backends[0]
    # advertised/tokens_published are the fourth cost reader (Task 8); their
    # exact values are covered by test_health.py, so only shape is checked
    # here, not to duplicate that assertion.
    assert record["advertised"] > 0
    assert record["tokens_published"] > 0
    assert {k: v for k, v in record.items() if k not in ("advertised", "tokens_published")} == {
        "name": "faketool",
        "attach": "ok",
        "catalogue": "ok",
        "probe": "none",
        # A surface with no [identity] table says so explicitly rather than
        # omitting the field: an operator diffing two surfaces should not have
        # to know that absence means "shared".
        "identity": {"mode": "none"},
    }


def test_health_deep_exits_unavailable_when_a_backend_fails(tmp_path):
    env = _registry(tmp_path, DEAD_BACKEND)
    r = _run(["health", "--deep", "--json"], env=env)
    assert r.returncode == 6


def test_health_deep_still_emits_records_when_it_exits_nonzero(tmp_path):
    """The verdict goes to the exit code and the DETAIL still goes to stdout —
    an operator gets told which backend died, not merely that something did."""
    env = _registry(tmp_path, DEAD_BACKEND)
    r = _run(["health", "--deep", "--json"], env=env)
    record = json.loads(r.stdout)["backends"][0]
    assert record["attach"] == "failed"
    assert "no-such-binary-xyz" in record["error"]


def test_health_stays_shallow_without_deep(tmp_path):
    """Default health must NOT fan out: it would be as slow and as flaky as the
    slowest backend, and one backend's outage is not a gateway outage."""
    env = _registry(tmp_path, DEAD_BACKEND)
    r = _run(["health", "--json"], env=env)
    assert r.returncode == 0
    assert "backends" not in json.loads(r.stdout)


def test_health_deep_on_an_empty_registry_is_ok(tmp_path):
    env = _env(tmp_path)
    r = _run(["health", "--deep", "--json"], env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["backends"] == []


def test_no_args_renders_dashboard():
    r = _run([])
    assert r.returncode == 0


def test_bad_usage_exits_2():
    assert _run(["definitely-not-a-verb"]).returncode == 2


def test_cli_search_emits_descriptions(fake_cli_cmd, tmp_path):
    """`beherouter search` and the MCP surface must agree on hit shape."""
    from beherouter.registry import RegistryEntry, save_registry

    reg_path = tmp_path / "registry.toml"
    save_registry(
        reg_path,
        {
            "faketool": RegistryEntry(
                name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd}
            )
        },
    )
    r = _run(["search", "--json", "faketool", "search"], env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    hit = json.loads(r.stdout)["hits"][0]
    assert hit == {"name": "faketool_search", "brief": "search things",
                   "mutating": False, "pinned": True}


def test_context_cost_reports_every_surface(fake_cli_cmd, tmp_path, monkeypatch, capsys):
    """One record per surface, in registry order."""
    from beherouter.cli.app import app

    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[faketool]\nplugin = "_test-cli"\n\n[faketool.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    code = app.main(["context-cost", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True
    record = out["surfaces"][0]
    assert record["name"] == "faketool"
    assert record["tokens_published"] > 0
    assert record["exact"] is False


def test_context_cost_accepts_a_window(fake_cli_cmd, tmp_path, monkeypatch, capsys):
    from beherouter.cli.app import app

    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[faketool]\nplugin = "_test-cli"\n\n[faketool.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    app.main(["context-cost", "--context-window", "200000", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["surfaces"][0]["pct_published"] > 0


def test_context_cost_costing_failure_does_not_hide_other_surfaces(
    fake_cli_cmd, tmp_path, monkeypatch, capsys
):
    """A `surface_cost` bug in one surface must not abort the whole sweep.

    Unlike the dead-backend case above (where `load_backend` itself raises),
    this attaches cleanly and fails only in costing -- the exact class of bug
    `gateway.build_surfaces` and `health.check_entry` already degrade instead
    of raising. `context_cost`'s own docstring promises the same.
    """
    from beherouter import costing
    from beherouter.cli.app import app

    real_surface_cost = costing.surface_cost

    async def flaky(mcp, backend):
        if backend.name == "broken":
            raise RuntimeError("boom")
        return await real_surface_cost(mcp, backend)

    monkeypatch.setattr(costing, "surface_cost", flaky)

    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[good]\nplugin = "_test-cli"\n\n[good.config]\ncmd = "{fake_cli_cmd}"\n\n'
        f'[broken]\nplugin = "_test-cli"\n\n[broken.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    monkeypatch.setenv("BEHEROUTER_TEST_PLUGINS", "1")
    code = app.main(["context-cost", "--json"])
    out = json.loads(capsys.readouterr().out)
    names = {r["name"]: r for r in out["surfaces"]}
    assert names["good"]["tokens_published"] > 0
    assert "error" in names["broken"]
    assert out["ok"] is False
    assert code != 0


def test_context_cost_reports_a_dead_backend_without_hiding_the_others(
    fake_cli_cmd, tmp_path, monkeypatch, capsys
):
    """health --deep's contract: one dead backend must not hide the rest."""
    from beherouter.cli.app import app

    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[good]\nplugin = "_test-cli"\n\n[good.config]\ncmd = "{fake_cli_cmd}"\n\n'
        f'[bad]\nplugin = "_test-cli"\n\n[bad.config]\ncmd = "/nonexistent/binary"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    code = app.main(["context-cost", "--json"])
    out = json.loads(capsys.readouterr().out)
    names = {r["name"]: r for r in out["surfaces"]}
    assert names["good"]["tokens_published"] > 0
    assert "error" in names["bad"]
    assert out["ok"] is False
    assert code != 0


def test_context_cost_unknown_surface_exits_not_found(tmp_path, fake_cli_cmd):
    """`--surface` names a surface that is not in the registry. NotFound == 3.

    The guard is worth a test because the alternative failure is silent: without
    it the filter would yield an empty registry and the command would exit 0
    reporting no surfaces, which reads as "this surface costs nothing".
    """
    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[faketool]\nplugin = "_test-cli"\n\n[faketool.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    r = _run(["context-cost", "--surface", "ghost"], env=_env(tmp_path))
    assert r.returncode == 3


def test_context_cost_on_an_empty_registry_reports_nothing_and_succeeds(tmp_path):
    """No surfaces attached is not an error: zero cost, cleanly reported."""
    r = _run(["context-cost", "--json"], env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True
    assert out["surfaces"] == []


def test_context_cost_closes_every_backend_it_built(
    fake_cli_cmd, tmp_path, monkeypatch, capsys
):
    """Each measured backend is dropped at the end of its iteration, so its
    executor's `aclose` (a kept-alive subprocess, an HTTP client) is awaited --
    including when costing fails after the attach succeeded."""
    from beherouter import costing
    from beherouter.cli import app as cli

    closed: list[str] = []
    real_load = cli.load_backend

    async def load(entry):
        backend = await real_load(entry)

        async def aclose(name=entry.name):
            closed.append(name)

        monkeypatch.setattr(backend.executor, "aclose", aclose, raising=False)
        return backend

    real_surface_cost = costing.surface_cost

    async def flaky(mcp, backend):
        if backend.name == "broken":
            raise RuntimeError("boom")
        return await real_surface_cost(mcp, backend)

    monkeypatch.setattr(cli, "load_backend", load)
    monkeypatch.setattr(costing, "surface_cost", flaky)
    reg = tmp_path / "registry.toml"
    reg.write_text(
        f'[good]\nplugin = "_test-cli"\n\n[good.config]\ncmd = "{fake_cli_cmd}"\n\n'
        f'[broken]\nplugin = "_test-cli"\n\n[broken.config]\ncmd = "{fake_cli_cmd}"\n'
    )
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(reg))
    cli.app.main(["context-cost", "--json"])
    capsys.readouterr()
    assert closed == ["good", "broken"]


# --- in-process: the same verbs, run where coverage can see them ------------

FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"


def _main(args, monkeypatch, capsys, tmp_path):
    from beherouter.cli.app import app

    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(tmp_path / "registry.toml"))
    code = app.main([*args, "--json"])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def test_the_version_of_a_bare_source_tree_is_unknown(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from beherouter.cli import app as cli

    def not_installed(_name):
        raise PackageNotFoundError("beherouter")

    monkeypatch.setattr(cli, "_dist_version", not_installed)
    assert cli._version() == "0+unknown"


def test_attach_search_surfaces_detach_in_process(tmp_path, fake_cli_cmd, monkeypatch, capsys):
    """The attach path writes the registry only after a load succeeded, into a
    directory it creates."""
    nested = tmp_path / "deep" / "dir"
    code, out = _main(["attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}"],
                      monkeypatch, capsys, nested)
    assert code == 0 and out["attached"] == "faketool" and out["tools"] > 0
    assert (nested / "registry.toml").is_file()
    code, out = _main(["surfaces"], monkeypatch, capsys, nested)
    assert out == {"surfaces": [{"name": "faketool", "plugin": "_test-cli"}]}
    code, out = _main(["search", "faketool", "search"], monkeypatch, capsys, nested)
    assert code == 0 and out["hits"][0]["name"] == "faketool_search"
    code, out = _main(["search", "ghost", "x"], monkeypatch, capsys, nested)
    assert code == 3
    code, out = _main(["detach", "faketool"], monkeypatch, capsys, nested)
    assert code == 0 and out == {"detached": "faketool"}
    code, _ = _main(["detach", "faketool"], monkeypatch, capsys, nested)
    assert code == 3


def test_read_bearer_strips_a_scheme_and_refuses_an_empty_file(tmp_path, monkeypatch):
    import io

    import pytest

    from beherouter.cli.app import _read_bearer
    from beherouter.errors import UsageError

    f = tmp_path / "tok"
    f.write_text("Bearer  abc \n")
    assert _read_bearer(str(f)) == "abc"
    f.write_text("plain\n")
    assert _read_bearer(str(f)) == "plain"
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin"))
    assert _read_bearer("-") == "from-stdin"
    f.write_text("  \n")
    with pytest.raises(UsageError, match="holds no token"):
        _read_bearer(str(f))


def _write_registry(tmp_path, fake_cli_cmd):
    (tmp_path / "registry.toml").write_text(
        f'[faketool]\nplugin = "_test-cli"\n\n[faketool.config]\ncmd = "{fake_cli_cmd}"\n\n'
        + DEAD_BACKEND
    )


def test_health_surface_filter_and_its_refusals(tmp_path, fake_cli_cmd, monkeypatch, capsys):
    _write_registry(tmp_path, fake_cli_cmd)
    code, out = _main(["health", "--surface", "faketool"], monkeypatch, capsys, tmp_path)
    assert code == 0 and out == {"ok": True, "count": 1}
    code, _ = _main(["health", "--surface", "nope"], monkeypatch, capsys, tmp_path)
    assert code == 3
    code, _ = _main(["health", "--bearer-file", "x"], monkeypatch, capsys, tmp_path)
    assert code == 2
    code, _ = _main(["health", "--textfile", str(tmp_path / "t.prom")],
                    monkeypatch, capsys, tmp_path)
    assert code == 2
    code, _ = _main(["health", "--deep", "--textfile", str(tmp_path / "no/dir/t.prom")],
                    monkeypatch, capsys, tmp_path)
    assert code == 2
    assert not (tmp_path / "no").exists()


def test_health_deep_writes_the_textfile_even_when_red(tmp_path, fake_cli_cmd, monkeypatch,
                                                       capsys):
    _write_registry(tmp_path, fake_cli_cmd)
    prom = tmp_path / "beherouter.prom"
    code, out = _main(["health", "--deep", "--textfile", str(prom)], monkeypatch, capsys,
                      tmp_path)
    assert code == 6 and out["ok"] is False
    assert {b["name"] for b in out["backends"]} == {"faketool", "ghost"}
    assert prom.is_file() and "ghost" in prom.read_text()


def test_health_deep_as_a_user_reads_the_bearer_file(tmp_path, fake_cli_cmd, monkeypatch,
                                                     capsys):
    from beherouter.cli import app as cli

    seen = {}

    async def deep(reg, user_token=None):
        seen["token"] = user_token
        return [{"name": n, "attach": "ok"} for n in reg]

    monkeypatch.setattr(cli, "deep_health", deep)
    monkeypatch.setattr(cli, "failed", lambda records: [])
    _write_registry(tmp_path, fake_cli_cmd)
    tok = tmp_path / "tok"
    tok.write_text("t0k\n")
    code, out = _main(["health", "--deep", "--surface", "faketool", "--bearer-file", str(tok)],
                      monkeypatch, capsys, tmp_path)
    assert code == 0 and out["ok"] is True and seen == {"token": "t0k"}


def test_context_cost_for_one_surface_and_a_failing_close(tmp_path, fake_cli_cmd, monkeypatch,
                                                          capsys):
    """A backend whose close raises is logged, never fatal to the sweep."""
    from beherouter.cli import app as cli

    real_load = cli.load_backend

    async def load(entry):
        backend = await real_load(entry)

        async def aclose():
            raise RuntimeError("close failed")

        monkeypatch.setattr(backend.executor, "aclose", aclose, raising=False)
        return backend

    monkeypatch.setattr(cli, "load_backend", load)
    _write_registry(tmp_path, fake_cli_cmd)
    code, out = _main(["context-cost", "--surface", "faketool"], monkeypatch, capsys, tmp_path)
    assert code == 0 and [r["name"] for r in out["surfaces"]] == ["faketool"]
    code, _ = _main(["context-cost", "--surface", "ghost-x"], monkeypatch, capsys, tmp_path)
    assert code == 3


def test_plugins_lists_tiers_and_the_note_for_a_capped_claim(tmp_path, monkeypatch, capsys):
    from beherouter.plugins import PLUGINS

    name = sorted(PLUGINS)[0]
    monkeypatch.setattr("beherouter.maturity.displayed_tier",
                        lambda p: ("probed", "capped") if p is PLUGINS[name] else
                        (p.spec.maturity, None))
    code, out = _main(["plugins"], monkeypatch, capsys, tmp_path)
    assert code == 0 and isinstance(out["failed"], list)
    rows = {r["name"]: r for r in out["plugins"]}
    assert rows[name]["maturity_note"] == "capped"
    assert all("maturity_note" not in r for n, r in rows.items() if n != name)


def test_plugin_config_and_the_catalog_verbs(tmp_path, monkeypatch, capsys):
    code, out = _main(["plugin-config", "pl", "plane"], monkeypatch, capsys, tmp_path)
    assert code == 0 and out
    server = FIXTURES / "server_json" / "notion-remote.json"
    code, out = _main(["catalog-import", str(server), "notion"], monkeypatch, capsys, tmp_path)
    assert code == 0 and out
    code, out = _main(["catalog-export", "office-mcp"], monkeypatch, capsys, tmp_path)
    assert code == 0 and out


def test_client_config_names_every_attached_surface(tmp_path, fake_cli_cmd, monkeypatch,
                                                    capsys):
    _write_registry(tmp_path, fake_cli_cmd)
    code, out = _main(["client-config", "claude-code", "--base-url", "https://gw.example.test"],
                      monkeypatch, capsys, tmp_path)
    assert code == 0 and "faketool" in json.dumps(out)


def test_serve_and_calendar_consent_hand_off_to_their_modules(tmp_path, monkeypatch, capsys):
    import beherouter.gateway as gateway
    import beherouter.plugins.calendar.consent as consent

    calls = {}
    monkeypatch.setattr(gateway, "serve",
                        lambda path, host, port: calls.setdefault("serve", (path, host, port)))
    code, _ = _main(["serve", "--port", "47999"], monkeypatch, capsys, tmp_path)
    assert code == 0
    assert calls["serve"] == (tmp_path / "registry.toml", gateway.DEFAULT_HOST, 47999)

    def run(reg, surface, **kw):
        calls["consent"] = (surface, kw)
        return {"surface": surface}

    monkeypatch.setattr(consent, "run", run)
    code, out = _main(["calendar-consent", "cal", "--subject", "a@x.test", "--no-browser",
                       "--revoke"], monkeypatch, capsys, tmp_path)
    assert code == 0 and out == {"surface": "cal"}
    surface, kw = calls["consent"]
    assert surface == "cal" and kw["no_browser"] is True and kw["revoke_grant"] is True


def test_main_answers_version_itself_and_hands_the_rest_to_beheaxi(tmp_path, monkeypatch,
                                                                   capsys):
    import pytest

    from beherouter.cli.app import app, main

    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"beherouter {app.version}"
    with pytest.raises(SystemExit) as e:
        main(["--version", "--json"])
    assert json.loads(capsys.readouterr().out) == {"tool": "beherouter", "version": app.version}
    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(tmp_path / "registry.toml"))
    monkeypatch.setattr("sys.argv", ["beherouter", "surfaces", "--json"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0 and json.loads(capsys.readouterr().out) == {"surfaces": []}


def _gcal_lookup(path_line: str) -> str:
    return (
        '[gcal]\nplugin = "gcal"\n'
        "  [gcal.env]\n"
        '  client_id = "id"\n  client_secret = "s"\n  refresh_token = "rt"\n'
        "  [gcal.identity]\n"
        '  mode = "lookup"\n  key = "email"\n'
        f"{path_line}"
        "    [gcal.identity.map]\n"
        '    refresh_token = "refresh_token"\n'
    )


def test_lint_refuses_an_identity_map_that_does_not_parse(tmp_path, monkeypatch):
    import pytest

    from beherouter.cli.app import registry_lint
    from beherouter.errors import UsageError

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    bad = tmp_path / "map.toml"
    bad.write_text("this is [not toml\n")
    reg = tmp_path / "registry.toml"
    reg.write_text(_gcal_lookup(f'  path = "{bad}"\n'))
    with pytest.raises(UsageError, match="is unparsable"):
        registry_lint(path=str(reg))


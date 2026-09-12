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
    assert hit["name"] == "faketool_search"
    assert hit["summary"] == "search things"
    assert hit["pinned"] is True
    assert hit["mutating"] is False


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

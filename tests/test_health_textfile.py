"""`health --deep --textfile PATH`: the scheduled deep-health contract.

Prometheus only ever probed the shallow `/healthz`, which stays green through a
revoked credential (the 2026-07-30 incident). A timer running this verb and
node_exporter's textfile collector reading its output is what watches for it.
"""

import json
import os
import subprocess
import sys


def _run(args, env):
    return subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _env(tmp_path, body):
    (tmp_path / "registry.toml").write_text(body)
    return {
        **os.environ,
        "BEHEROUTER_TEST_PLUGINS": "1",
        "BEHEROUTER_REGISTRY": str(tmp_path / "registry.toml"),
    }


DEAD = '[ghost]\nplugin = "_test-cli"\n  [ghost.config]\n  cmd = "no-such-binary-xyz"\n'


def test_a_green_sweep_writes_the_file(tmp_path, fake_cli_cmd):
    env = _env(
        tmp_path,
        f'[faketool]\nplugin = "_test-cli"\n  [faketool.config]\n  cmd = "{fake_cli_cmd}"\n',
    )
    out = tmp_path / "prom" / "beherouter.prom"
    out.parent.mkdir()
    r = _run(["health", "--deep", "--json", "--textfile", str(out)], env)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert 'beherouter_surface_attach_ok{surface="faketool"} 1' in text
    assert "beherouter_health_ok 1" in text
    assert json.loads(r.stdout)["ok"] is True


def test_a_failing_sweep_still_writes_the_file_then_exits_6(tmp_path):
    """The file is the verdict a monitor reads; the exit code is for a human.
    Writing only on success would leave the LAST GREEN file in place."""
    env = _env(tmp_path, DEAD)
    out = tmp_path / "beherouter.prom"
    r = _run(["health", "--deep", "--textfile", str(out)], env)
    assert r.returncode == 6
    text = out.read_text()
    assert 'beherouter_surface_attach_ok{surface="ghost"} 0' in text
    assert "beherouter_health_ok 0" in text


def test_textfile_needs_deep(tmp_path):
    env = _env(tmp_path, "")
    r = _run(["health", "--textfile", str(tmp_path / "x.prom")], env)
    assert r.returncode == 2
    assert not (tmp_path / "x.prom").exists()


def test_textfile_into_a_missing_directory_is_a_usage_error(tmp_path):
    """Creating the collector's directory is the collector's job; a typo in
    the path must not quietly make a directory nothing scrapes."""
    env = _env(tmp_path, "")
    r = _run(["health", "--deep", "--textfile", str(tmp_path / "nope" / "x.prom")], env)
    assert r.returncode == 2

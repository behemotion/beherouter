"""The CLI's version is the package's version (CONVENTIONS §7).

It used to be a literal `0.1.0` in `cli/app.py`, three releases stale: every
manifest a sibling read from `describe --json` named a version that had never
shipped. Read from metadata, it cannot drift from pyproject again.
"""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_app_object_carries_the_pyproject_version():
    from beherouter.cli.app import app

    assert app.version == _pyproject_version()


def test_describe_reports_the_pyproject_version():
    r = _run("describe", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["version"] == _pyproject_version()


def test_dash_dash_version_prints_it():
    r = _run("--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"beherouter {_pyproject_version()}"


def test_dash_dash_version_honours_json():
    r = _run("--version", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"tool": "beherouter", "version": _pyproject_version()}

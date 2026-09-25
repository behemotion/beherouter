"""The chart's `plugins.install` init container: install a plugin WITHOUT
shadowing the gateway.

A bare `uv pip install --target` installs fresh copies of every shared
dependency, and PYTHONPATH puts them ahead of the gateway's own — one plugin
install would silently upgrade httpx or fastmcp underneath the gateway.
Measured 2026-09-24: `--target` with `respx` pulled a newer anyio beside the
gateway's. These tests pin the three rules that prevent it, offline.
"""

import sys
from pathlib import Path

import pytest

from beherouter import plugininstall as pi


def test_index_distributions_are_pinned_at_the_gateways_version(tmp_path):
    have = {"httpx": ("0.28.1", True), "beheaxi": ("0.1.1", False)}
    constraints, overrides = pi.requirement_files(have, tmp_path)
    assert constraints.read_text().split() == ["httpx==0.28.1"]
    assert 'beheaxi; sys_platform == "never"' in overrides.read_text()


def test_beherouter_itself_is_never_offered_to_an_index(tmp_path):
    constraints, overrides = pi.requirement_files({"beherouter": ("0.2.2", True)}, tmp_path)
    assert "beherouter" not in constraints.read_text()
    assert "beherouter" in overrides.read_text()


def test_this_environment_knows_the_gateway_and_its_git_pinned_dependency():
    have = pi.gateway_distributions()
    assert "fastmcp" in have and have["fastmcp"][1] is True
    assert "beherouter" in have


def _fake_dist(target: Path, name: str, version: str, files: list[str]) -> None:
    info = target / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    for rel in files:
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    record = [*files, f"{info.name}/RECORD"]
    (info / "RECORD").write_text("\n".join(f"{r},," for r in record))


def test_prune_leaves_only_what_the_plugin_adds(tmp_path):
    _fake_dist(tmp_path, "httpx", "0.28.1", ["httpx/__init__.py", "httpx/_api.py"])
    _fake_dist(tmp_path, "acme_crm", "0.1.0", ["acme_crm/__init__.py"])
    removed = pi.prune(tmp_path, {"httpx": ("0.28.1", True)})
    assert removed == ["httpx"]
    assert not (tmp_path / "httpx").exists()
    assert (tmp_path / "acme_crm" / "__init__.py").exists()


def test_prune_never_leaves_the_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    target = tmp_path / "t"
    info = target / "evil-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "RECORD").write_text("../outside.txt,,\n")
    pi.prune(target, {"evil": ("1.0", True)})
    assert outside.exists()


def test_install_passes_constraints_and_overrides_to_uv(tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        target = Path(cmd[cmd.index("--target") + 1])
        _fake_dist(target, "acme_crm", "0.1.0", ["acme_crm/__init__.py"])
        _fake_dist(target, "fastmcp", "3.4.5", ["fastmcp/__init__.py"])

        class P:
            returncode = 0

        return P()

    out = pi.install(tmp_path / "plugins", ["acme-crm==0.1.0"], run=fake_run)
    cmd = seen["cmd"]
    assert cmd[:3] == ["uv", "pip", "install"]
    assert cmd[cmd.index("--python") + 1] == sys.executable
    assert "--constraints" in cmd and "--overrides" in cmd
    assert cmd[-1] == "acme-crm==0.1.0"
    assert out["added"] == ["acme_crm-0.1.0"]
    assert "fastmcp" in out["shared_with_gateway"]


def test_a_failed_install_stops_the_init_container(tmp_path):
    class P:
        returncode = 1

    with pytest.raises(SystemExit, match="conflict"):
        pi.install(tmp_path, ["acme-crm"], run=lambda cmd, **kw: P())


def test_nothing_to_install_is_a_no_op(tmp_path):
    assert pi.install(tmp_path / "p", [])["added"] == []
    assert not (tmp_path / "p").exists()

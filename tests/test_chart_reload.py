"""Chart wiring for hot reload, the kill switch and the admin API (spec §7)."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "charts" / "beherouter"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _helm(values: str, *sets: str) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "t", str(CHART), "-f", str(CHART / values)]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _render(values: str, *sets: str) -> list[dict]:
    r = _helm(values, *sets)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _deployment(docs):
    (d,) = [x for x in docs if x["kind"] == "Deployment"]
    return d


def _env(dep) -> dict:
    c = dep["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in c["env"]}


def test_defaults_keep_the_checksum_and_add_nothing():
    dep = _deployment(_render("ci/minimal.yaml"))
    ann = dep["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/configmap-registry" in ann
    env = _env(dep)
    for name in (
        "BEHEROUTER_ADMIN_TOKEN",
        "BEHEROUTER_ADMIN_ROLE",
        "BEHEROUTER_KILLSWITCH_PATH",
        "BEHEROUTER_REGISTRY_WATCH_S",
    ):
        assert name not in env
    spec = dep["spec"]["template"]["spec"]
    assert "fsGroup" not in spec["securityContext"]


def test_reload_values_wire_everything():
    docs = _render("ci/reload.yaml")
    dep = _deployment(docs)
    spec = dep["spec"]["template"]["spec"]
    ann = dep["spec"]["template"]["metadata"].get("annotations", {})
    assert "checksum/configmap-registry" not in ann
    env = _env(dep)
    assert env["BEHEROUTER_REGISTRY_WATCH_S"]["value"] == "30"
    ref = env["BEHEROUTER_ADMIN_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "BEHEROUTER_ADMIN_TOKEN"
    assert env["BEHEROUTER_ADMIN_ROLE"]["value"] == "gw-admin"
    assert env["BEHEROUTER_KILLSWITCH_PATH"]["value"] == "/var/lib/beherouter/killswitch.json"
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["killswitch"]["persistentVolumeClaim"]["claimName"] == "ks-claim"
    assert vols["secret-files"]["secret"]["secretName"] == "backend-secrets"
    mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert "subPath" not in mounts["secret-files"]  # a subPath mount never updates
    assert spec["securityContext"]["fsGroup"] == 1000
    (secret,) = [
        x for x in docs if x["kind"] == "Secret" and x["metadata"]["name"].endswith("-env")
    ]
    assert secret["stringData"]["BEHEROUTER_ADMIN_TOKEN"] == "ci-admin-token"
    # The pod reads the token from that very Secret.
    assert ref["name"] == secret["metadata"]["name"]


def test_an_explicit_fsgroup_is_kept():
    dep = _deployment(
        _render("ci/reload.yaml", "podSecurityContext.fsGroup=2000")
    )
    assert dep["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 2000


def test_admin_token_with_an_external_secret_fails_to_render():
    # Silently dropping it would boot a gateway with no admin credential.
    r = _helm("ci/reload.yaml", "secret.create=false", "existingSecret.name=mine")
    assert r.returncode != 0
    assert "admin.token" in r.stderr and "extraEnv" in r.stderr


def test_an_external_secret_without_an_admin_token_still_renders():
    docs = _render(
        "ci/reload.yaml", "secret.create=false", "existingSecret.name=mine",
        "admin.token=",
    )
    env = _env(_deployment(docs))
    assert "BEHEROUTER_ADMIN_TOKEN" not in env  # the operator supplies it via extraEnv
    assert env["BEHEROUTER_ADMIN_ROLE"]["value"] == "gw-admin"
    assert not [x for x in docs if x["kind"] == "Secret"]


def test_killswitch_without_a_claim_fails_to_render():
    r = _helm("ci/minimal.yaml", "killswitch.enabled=true")
    assert r.returncode != 0 and "killswitch.existingClaim" in r.stderr


def test_secret_files_without_a_secret_fails_to_render():
    r = _helm("ci/minimal.yaml", "secretFiles.enabled=true")
    assert r.returncode != 0 and "secretFiles.secretName" in r.stderr


def test_lint_hook_mounts_secret_files_like_the_deployment():
    docs = _render("ci/reload.yaml")
    job = next(
        d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-registry-lint")
    )
    spec = job["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["secret-files"]["secret"]["secretName"] == "backend-secrets"
    mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    dep = _deployment(docs)["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    dep_mount = next(m for m in dep if m["name"] == "secret-files")
    assert mounts["secret-files"]["mountPath"] == dep_mount["mountPath"]
    assert mounts["secret-files"]["readOnly"] is True
    assert "subPath" not in mounts["secret-files"]
    # The kill switch PVC stays out of the hook (an RWO claim can deadlock scheduling).
    assert "killswitch" not in vols
    names = {e["name"] for e in spec["containers"][0]["env"]}
    assert "BEHEROUTER_KILLSWITCH_PATH" not in names

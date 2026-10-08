"""The chart's opt-in scheduled deep health (healthCronJob).

Rendered with the real `helm` binary; skipped where it is not installed (CI's
helm job lints and schema-validates every ci/ scenario, ci/health.yaml
included).
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "charts" / "beherouter"
TEMPLATE = "templates/health-cronjob.yaml"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*sets: str, values: str = "ci/minimal.yaml", show_only: bool = True):
    cmd = ["helm", "template", "t", str(CHART), "-f", str(CHART / values)]
    for s in sets:
        cmd += ["--set", s]
    if show_only:
        cmd += ["-s", TEMPLATE]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _cronjob(*sets: str, values: str = "ci/minimal.yaml") -> dict:
    r = _render(*sets, values=values)
    assert r.returncode == 0, r.stderr
    return yaml.safe_load(r.stdout)


def test_disabled_by_default():
    r = _render(show_only=False)
    assert r.returncode == 0, r.stderr
    assert "kind: CronJob" not in r.stdout


def test_enabled_without_a_hostpath_refuses_to_render():
    r = _render("healthCronJob.enabled=true")
    assert r.returncode != 0
    assert "healthCronJob.textfile.hostPath is required" in r.stderr


def test_a_bearer_secret_without_a_key_refuses_to_render():
    r = _render(
        "healthCronJob.enabled=true",
        "healthCronJob.textfile.hostPath=/tf",
        "healthCronJob.bearerFile.secretName=probe",
    )
    assert r.returncode != 0
    assert "healthCronJob.bearerFile.key is required" in r.stderr


def test_enabled_renders_the_contrib_contract():
    cj = _cronjob("healthCronJob.enabled=true", "healthCronJob.textfile.hostPath=/tf")
    assert cj["kind"] == "CronJob"
    assert cj["spec"]["schedule"] == "*/5 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    job = cj["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0
    pod = job["template"]["spec"]
    (c,) = pod["containers"]
    script = c["args"][0]
    assert "beherouter health --deep" in script
    assert "--textfile /textfile/beherouter.prom" in script
    assert '[ "$rc" -eq 6 ]' in script  # a red sweep is a successful run
    assert "--bearer-file" not in script
    env = {e["name"]: e for e in c["env"]}
    # the gateway's own env helper: registry path, token from the release Secret
    assert env["BEHEROUTER_REGISTRY"]["value"] == "/data/registry.toml"
    secret_ref = env["BEHEROUTER_GATEWAY_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == "t-beherouter-env"
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["registry"]["configMap"]["name"] == "t-beherouter-registry"
    assert vols["textfile"]["hostPath"] == {"path": "/tf", "type": "Directory"}


def test_job_pods_do_not_carry_the_service_selector():
    """A Ready Job pod with name+instance labels would be routed gateway traffic."""
    cj = _cronjob("healthCronJob.enabled=true", "healthCronJob.textfile.hostPath=/tf")
    labels = cj["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert "app.kubernetes.io/name" not in labels


def test_full_scenario_reuses_the_shared_plumbing():
    cj = _cronjob(values="ci/health.yaml")
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (c,) = pod["containers"]
    assert "--bearer-file /etc/beherouter/probe-user/token" in c["args"][0]
    env = {e["name"] for e in c["env"]}
    assert {"SSL_CERT_FILE", "PYTHONPATH", "BEHEROUTER_IDENTITY_MAP"} <= env
    assert [i["name"] for i in pod["initContainers"]] == ["ca-bundle", "plugins"]
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["probe-user"]["secret"] == {
        "secretName": "beherouter-probe-user",
        "items": [{"key": "token.jwt", "path": "token"}],
    }
    assert {"ca-bundle", "ca-extra", "plugins", "identity-map"} <= set(vols)
    mounts = {m["name"] for m in c["volumeMounts"]}
    assert {"probe-user", "ca-bundle", "plugins", "identity-map", "textfile"} <= mounts
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-1"}
    assert pod["tolerations"][0]["key"] == "dedicated"

"""The chart's plugin sources (client ask A1): `plugins.indexes[]` and
`plugins.local`, beside the original `plugins.install` / `indexUrl`.

Rendered with the real `helm` binary; skipped where it is not installed (CI's
helm job lints and schema-validates every ci/ scenario).
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "charts" / "beherouter"
LOCAL = "/etc/beherouter/plugins-local"

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


def _pods(docs) -> dict[str, dict]:
    """The Deployment's and the lint hook's pod specs, by kind."""
    out = {}
    for d in docs:
        if d["kind"] == "Deployment":
            out["Deployment"] = d["spec"]["template"]["spec"]
        elif d["kind"] == "Job":
            out["Job"] = d["spec"]["template"]["spec"]
    assert set(out) == {"Deployment", "Job"}
    return out


def _init(pod) -> dict:
    (c,) = [i for i in pod.get("initContainers", []) if i["name"] == "plugins"]
    return c


def _env(c) -> dict:
    return {e["name"]: e for e in c["env"]}


def test_no_plugins_renders_no_init_container():
    for pod in _pods(_render("ci/minimal.yaml")).values():
        assert "initContainers" not in pod
        assert "plugins" not in {v["name"] for v in pod["volumes"]}


def test_indexes_render_as_named_uv_indexes_with_their_own_credentials():
    for kind, pod in _pods(_render("ci/plugins-indexes.yaml")).items():
        env = _env(_init(pod))
        # indexUrl is unchanged: the default (fallback) index, named `plugins`.
        assert env["UV_DEFAULT_INDEX"]["value"] == "plugins=https://pypi.corp.example.com/simple"
        assert env["UV_INDEX_PLUGINS_USERNAME"]["valueFrom"]["secretKeyRef"]["name"] == "corp-pypi"
        assert env["UV_INDEX"]["value"] == (
            "back-office=https://gitlab.example.com/api/v4/projects/1234/packages/pypi/simple"
            " mirror2=https://mirror.example.com/simple"
        ), kind
        for part in ("USERNAME", "PASSWORD"):
            ref = env[f"UV_INDEX_BACK_OFFICE_{part}"]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": "gitlab-back-office", "key": part.lower()}
        # An index without a Secret gets no credential variables.
        assert not [n for n in env if n.startswith("UV_INDEX_MIRROR2_")]
        # uv's default first-index strategy is what keeps dependency confusion
        # out; the chart must not loosen it.
        assert "UV_INDEX_STRATEGY" not in env


def test_local_wheels_install_from_an_optional_configmap_mount():
    for kind, pod in _pods(_render("ci/plugins-local.yaml")).items():
        c = _init(pod)
        wheel = f"{LOCAL}/dwh-wheels/beherouter_dwh-0.3.0-py3-none-any.whl"
        assert c["command"][:3] == ["python", "-m", "beherouter.plugininstall"]
        assert c["command"][-1] == wheel, kind
        mounts = {m["name"]: m for m in c["volumeMounts"]}
        assert mounts["plugins-local"] == {
            "name": "plugins-local",
            "mountPath": f"{LOCAL}/dwh-wheels",
            "readOnly": True,
        }
        vols = {v["name"]: v for v in pod["volumes"]}
        # optional: a missing ConfigMap fails the init container by name
        # (plugininstall), instead of leaving the pod in ContainerCreating.
        assert vols["plugins-local"]["configMap"] == {"name": "dwh-wheels", "optional": True}
        assert "plugins" in vols
        # The gateway sees the installed plugin, not the wheel.
        gw = pod["containers"][0]
        assert "plugins-local" not in {m["name"] for m in gw["volumeMounts"]}
        assert {e["name"]: e for e in gw["env"]}["PYTHONPATH"]["value"] == "/opt/beherouter/plugins"


def test_a_new_wheel_version_changes_the_pod_template():
    def template(wheel):
        (dep,) = [
            d
            for d in _render("ci/plugins-local.yaml", f"plugins.local.wheels[0]={wheel}")
            if d["kind"] == "Deployment"
        ]
        return dep["spec"]["template"]

    assert template("beherouter_dwh-0.3.0-py3-none-any.whl") != template(
        "beherouter_dwh-0.3.1-py3-none-any.whl"
    )


def test_install_and_local_together_share_one_init_container():
    pod = _pods(
        _render("ci/plugins-local.yaml", "plugins.install[0]=acme-beherouter-crm==0.1.0")
    )["Deployment"]
    cmd = _init(pod)["command"]
    assert "acme-beherouter-crm==0.1.0" in cmd
    assert cmd[-1].endswith("beherouter_dwh-0.3.0-py3-none-any.whl")


@pytest.mark.parametrize(
    "sets, message",
    [
        (["plugins.indexes[0].name=Back_Office", "plugins.indexes[0].url=https://x"], "[a-z0-9-]+"),
        (["plugins.indexes[0].name=plugins", "plugins.indexes[0].url=https://x"], "reserved"),
        (
            [
                "plugins.indexes[0].name=a",
                "plugins.indexes[0].url=https://x",
                "plugins.indexes[1].name=a",
                "plugins.indexes[1].url=https://y",
            ],
            "duplicate",
        ),
        (["plugins.indexes[0].name=a"], "url"),
        (["plugins.local.wheels[0]=dwh-0.3.0-py3-none-any.whl"], "plugins.local.configMap"),
        (
            ["plugins.local.configMap=w", "plugins.local.wheels[0]=dwh-0.3.0.tar.gz"],
            "wheel",
        ),
        (
            ["plugins.local.configMap=w", "plugins.local.wheels[0]=sub/dwh-0.3.0-py3-none-any.whl"],
            "wheel",
        ),
    ],
)
def test_invalid_plugin_sources_refuse_to_render(sets, message):
    r = _helm("ci/minimal.yaml", "plugins.install[0]=acme==1", *sets)
    assert r.returncode != 0
    assert message in r.stderr

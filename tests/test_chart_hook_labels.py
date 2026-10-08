"""Hook pods must not match the gateway's selector.

The Service, the PodDisruptionBudget and the NetworkPolicy all select on the
chart's selector labels (name + instance). A hook pod carrying both would be
counted as a gateway replica while it runs: a Ready registry-lint or
test-connection pod is an endpoint candidate and a PDB member. The
Deployment's own selector is immutable, so the fix is on the hook pods, and
this pins it.

Rendered with the real `helm` binary; skipped where it is not installed (CI's
helm job has it).
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "charts" / "beherouter"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _docs(values: str = "ci/minimal.yaml") -> list[dict]:
    r = subprocess.run(
        ["helm", "template", "t", str(CHART), "-f", str(CHART / values)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _one(docs: list[dict], kind: str, suffix: str = "") -> dict:
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    return doc


def _matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


@pytest.mark.parametrize("values", ["ci/minimal.yaml", "ci/full.yaml"])
def test_hook_pods_are_not_selected_as_gateway_replicas(values: str):
    docs = _docs(values)
    service_selector = _one(docs, "Service")["spec"]["selector"]
    deployment = _one(docs, "Deployment")
    gateway_pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    # Sanity: the selector really does select the gateway, so a non-match
    # below is meaningful rather than vacuous.
    assert _matches(service_selector, gateway_pod_labels)
    assert deployment["spec"]["selector"]["matchLabels"] == service_selector

    lint_job = _one(docs, "Job", "-registry-lint")
    lint_pod_labels = lint_job["spec"]["template"]["metadata"]["labels"]
    assert not _matches(service_selector, lint_pod_labels)
    assert lint_pod_labels["app.kubernetes.io/component"] == "registry-lint"
    assert lint_pod_labels["app.kubernetes.io/instance"] == "t"

    test_pod = _one(docs, "Pod", "-test-connection")
    assert not _matches(service_selector, test_pod["metadata"]["labels"])


def test_selector_is_unchanged_for_upgrades():
    """A Deployment's selector is immutable: changing it breaks `helm upgrade`."""
    deployment = _one(_docs(), "Deployment")
    assert deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "beherouter",
        "app.kubernetes.io/instance": "t",
    }

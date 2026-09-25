"""Dependency bounds that protect the plugin API.

FastMCP 4.0 (2026-08-31) moved the proxy and OpenAPI modules and renamed
`mount(prefix=)`; with no upper bound, a plain `pip install beherouter`
resolves it. The cap moves only together with a port (one module:
`backends/inproc.py`, per the plugin-sources spec).
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _requirement(name: str) -> Requirement:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return next(r for r in map(Requirement, deps) if r.name == name)


def test_fastmcp_is_capped_below_4():
    spec = _requirement("fastmcp").specifier
    assert not spec.contains("4.0.0"), f"fastmcp {spec} admits 4.x"
    assert spec.contains("3.4.5"), f"fastmcp {spec} excludes the locked 3.4.5"

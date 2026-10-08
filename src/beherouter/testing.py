"""Plugin conformance: is a plugin's declared maturity tier backed by evidence?

    from beherouter.testing import plugin_conformance

    def test_my_plugin_meets_its_tier():
        report = plugin_conformance(SPEC)          # root defaults to the cwd
        assert report.ok, report.problems

The same checks the in-tree suite runs over every plugin in `PLUGINS`
(tests/test_maturity.py), shipped for out-of-tree authors — the Singer-SDK
pattern: one conformance suite, two audiences.

Tiers are cumulative; each includes every check below it:

| tier         | checked by                                                         |
|--------------|--------------------------------------------------------------------|
| `declared`   | nothing: a spec exists                                             |
| `probed`     | a default `probe`, present in a cited recorded catalogue, whose    |
|              | `probe_args` (or `{}`) VALIDATE against that tool's inputSchema    |
| `catalogued` | every pin and every `search_aliases` key is in the catalogue       |
| `verified`   | an `evidence` test id under an `e2e/` directory that resolves      |
| `per-user`   | declared `IdentitySupport` and a cited e2e check asserting         |
|              | `matches_caller`                                                   |

Evidence items, relative to `root`:

- `path/to/catalogue.json` — a recorded `tools/list` reply: a JSON array of
  `{name, description, inputSchema, ...}` rows (`scripts/record_catalogue.py`).
- `path/to/test_file.py::test_name` — a pytest function, or
  `path/to/driver.py::<check name>` — a named `check(cond, "name", ...)` in an
  e2e driver script such as tests/e2e/e2e.py, which is not a pytest module.

⚠️ An evidence item that does not resolve is a problem EVEN WHEN the tier is
otherwise met: stale evidence is how a tier silently stops being true.

⚠️ What this cannot check is that a cited e2e check exercises THIS plugin. The
id must exist, and for `per-user` must assert `matches_caller`; that it is the
right check is a code-review question, which is why the id is spelled out on
the spec rather than discovered.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from .plugins.spec import MATURITY_TIERS, PluginSpec

__all__ = ["Conformance", "plugin_conformance"]


@dataclass(frozen=True)
class Conformance:
    plugin: str
    declared: str
    # The highest tier the evidence supports (it may exceed `declared`).
    met: str
    # Why the first unmet tier is unmet, plus every evidence item that does
    # not resolve. Empty when the plugin is at its ceiling with clean evidence.
    problems: tuple[str, ...] = field(default_factory=tuple)
    # Evidence items that did not resolve; any one fails `ok`.
    broken: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return (
            self.declared in MATURITY_TIERS
            and MATURITY_TIERS.index(self.met) >= MATURITY_TIERS.index(self.declared)
            and not self.broken
        )


@dataclass(frozen=True)
class _TestId:
    item: str
    path: Path
    name: str
    asserts_matches_caller: bool


def _resolve_test_id(root: Path, item: str) -> _TestId | str:
    """A resolved test id, or the reason it does not resolve."""
    rel, _, name = item.partition("::")
    path = root / rel
    if not name:
        return f"evidence '{item}': a test id needs `path::name`"
    if not path.is_file():
        return f"evidence '{item}': {rel} does not exist under {root}"
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        return f"evidence '{item}': {rel} does not parse ({e.msg})"
    for node in ast.walk(tree):
        # A pytest function (top-level or in a class: `Class::test` is
        # matched on its last segment, which pytest ids end with).
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name.rsplit("::", 1)[-1]
        ):
            return _TestId(item, path, name, "matches_caller" in ast.unparse(node))
        # A named check in an e2e driver: check(<condition>, "<name>", ...).
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == name
        ):
            return _TestId(item, path, name, "matches_caller" in ast.unparse(node.args[0]))
    return f"evidence '{item}': no test function or named check '{name}' in {rel}"


def _load_catalogue(root: Path, item: str) -> dict[str, dict] | str:
    path = root / item
    if not path.is_file():
        return f"evidence '{item}': recorded catalogue does not exist under {root}"
    try:
        rows = json.loads(path.read_text())
        return {row["name"]: row for row in rows}
    except (ValueError, TypeError, KeyError) as e:
        return f"evidence '{item}': not a recorded tools/list array ({type(e).__name__})"


def _validate_args(schema: dict, args: dict) -> str | None:
    """None when `args` satisfy `schema`, else jsonschema's one-line reason."""
    import jsonschema

    cls = jsonschema.validators.validator_for(schema)
    error = jsonschema.exceptions.best_match(cls(schema).iter_errors(args))
    return None if error is None else error.message


def plugin_conformance(spec: PluginSpec, root: str | Path | None = None) -> Conformance:
    """Check `spec` against its declared tier. Pure: reads files, no network.

    `root` is what evidence paths are relative to — the repository (or
    distribution) the plugin ships in. Defaults to the working directory, which
    is the repo root under pytest.
    """
    root = Path.cwd() if root is None else Path(root)
    if spec.maturity not in MATURITY_TIERS:
        return Conformance(
            spec.name,
            spec.maturity,
            "declared",
            (f"maturity '{spec.maturity}' is not one of {MATURITY_TIERS}",),
        )

    broken: list[str] = []
    catalogue: dict[str, dict] = {}
    tests: list[_TestId] = []
    for item in spec.evidence:
        if "::" not in item and item.endswith(".json"):
            loaded = _load_catalogue(root, item)
            if isinstance(loaded, str):
                broken.append(loaded)
            else:
                catalogue.update(loaded)
            continue
        resolved = _resolve_test_id(root, item)
        if isinstance(resolved, str):
            broken.append(resolved)
        else:
            tests.append(resolved)

    def probed() -> list[str]:
        if not spec.probe:
            return ["no default `probe`"]
        if not catalogue:
            return ["no recorded catalogue in `evidence` to check the probe against"]
        row = catalogue.get(spec.probe)
        if row is None:
            return [f"probe '{spec.probe}' is not in the recorded catalogue"]
        reason = _validate_args(row.get("inputSchema") or {}, spec.probe_args or {})
        if reason:
            return [(
                f"probe_args {spec.probe_args!r} do not validate against "
                f"'{spec.probe}''s recorded schema: {reason}"
            )]
        return []

    def catalogued() -> list[str]:
        problems = [f"pinned '{t}' is not in the recorded catalogue"
                    for t in spec.pinned if t not in catalogue]
        problems += [f"search_aliases names '{t}', which is not in the recorded catalogue"
                     for t in spec.search_aliases if t not in catalogue]
        return problems

    def _is_e2e(t: _TestId) -> bool:
        return "e2e" in t.path.relative_to(root).parts[:-1]

    def verified() -> list[str]:
        if not any(_is_e2e(t) for t in tests):
            return ["no resolving e2e test id (a path under an `e2e/` directory) in `evidence`"]
        return []

    def per_user() -> list[str]:
        problems = []
        if not spec.identity.modes:
            problems.append("declares no IdentitySupport modes")
        if not any(_is_e2e(t) and t.asserts_matches_caller for t in tests):
            problems.append("no cited e2e check asserts `matches_caller`")
        return problems

    checks = {"probed": probed, "catalogued": catalogued,
              "verified": verified, "per-user": per_user}
    met, problems = "declared", []
    for tier in MATURITY_TIERS[1:]:
        problems = checks[tier]()
        if problems:
            problems = [f"not '{tier}': {p}" for p in problems]
            break
        met = tier
    return Conformance(spec.name, spec.maturity, met, tuple(problems + broken), tuple(broken))

"""Maturity tiers: a tier is a claim about evidence, held by a test.

The parametrized test is the in-tree half of `beherouter.testing`; the unit
tests below pin what each tier checks, against throwaway evidence trees, so a
rule cannot quietly loosen while every in-tree plugin still passes.
"""

import json
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from beherouter.errors import UsageError
from beherouter.maturity import displayed_tier, lint_warning
from beherouter.plugins import PLUGINS, Plugin, register
from beherouter.plugins.spec import MATURITY_TIERS, IdentitySupport, PluginSpec
from beherouter.testing import plugin_conformance

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", sorted(PLUGINS))
def test_every_in_tree_plugin_meets_its_declared_tier(name):
    report = plugin_conformance(PLUGINS[name].spec, root=REPO)
    assert report.ok, (
        f"{name} declares '{report.declared}', meets '{report.met}': {report.problems}"
    )


@pytest.mark.parametrize("name", sorted(PLUGINS))
def test_no_in_tree_plugin_undersells_its_evidence(name):
    """The converse: a tier left behind after evidence landed is a stale claim
    too, just a harmless-looking one. Raise the declaration with the evidence."""
    report = plugin_conformance(PLUGINS[name].spec, root=REPO)
    assert report.met == report.declared, (name, report.met, report.declared)


def test_generic_plugins_are_always_declared():
    for name, plugin in PLUGINS.items():
        if plugin.spec.requires_entry:
            assert plugin.spec.maturity == "declared", name


# --- the rules, against a throwaway evidence tree -------------------------

CATALOGUE = [
    {"name": "search", "description": "", "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["q"],
    }},
    {"name": "get", "description": "", "inputSchema": {"type": "object", "properties": {}}},
]

E2E = textwrap.dedent('''
    def check(ok, name, detail=""): ...
    def main():
        check(True, "it attaches for real")
        up = {}
        check(up.get("matches_caller") is True, "the caller is themselves")

    def test_pytest_style():
        assert True
''')


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "cat").mkdir()
    (tmp_path / "cat/x.json").write_text(json.dumps(CATALOGUE))
    (tmp_path / "tests/e2e").mkdir(parents=True)
    (tmp_path / "tests/e2e/e2e.py").write_text(E2E)
    (tmp_path / "tests/test_unit.py").write_text("def test_unit():\n    pass\n")
    return tmp_path


def _spec(**kw):
    base = {
        "name": "x", "summary": "x", "backing": "http",
        "pinned": ("search", "get"), "probe": "search",
        "probe_args": {"q": "a", "limit": 1}, "search_aliases": {"get": ("fetch",)},
    }
    return PluginSpec(**{**base, **kw})


def test_declared_needs_nothing(tree):
    report = plugin_conformance(_spec(probe=None, evidence=()), root=tree)
    assert report.ok and report.met == "declared"


def test_probed_needs_probe_args_that_validate_against_the_recorded_schema(tree):
    good = _spec(maturity="probed", evidence=("cat/x.json",), pinned=(), search_aliases={})
    assert plugin_conformance(good, root=tree).ok
    bad = replace(good, probe_args={"limit": 1})  # misses required `q`
    report = plugin_conformance(bad, root=tree)
    assert not report.ok and report.met == "declared"
    assert "'q' is a required property" in report.problems[0]


def test_probed_needs_a_catalogue_to_check_against(tree):
    report = plugin_conformance(_spec(maturity="probed"), root=tree)
    assert not report.ok and "no recorded catalogue" in report.problems[0]


def test_catalogued_needs_every_pin_and_alias_key_in_the_catalogue(tree):
    spec = _spec(maturity="catalogued", evidence=("cat/x.json",))
    assert plugin_conformance(spec, root=tree).ok
    report = plugin_conformance(
        replace(spec, pinned=("search", "gone"), search_aliases={"vanished": ("w",)}),
        root=tree,
    )
    assert report.met == "probed"
    assert any("'gone'" in p for p in report.problems)
    assert any("'vanished'" in p for p in report.problems)


def test_verified_needs_an_e2e_id_that_exists(tree):
    spec = _spec(maturity="verified",
                 evidence=("cat/x.json", "tests/e2e/e2e.py::it attaches for real"))
    assert plugin_conformance(spec, root=tree).ok
    # A unit test is not end-to-end evidence, however real it is.
    unit = replace(spec, evidence=("cat/x.json", "tests/test_unit.py::test_unit"))
    assert plugin_conformance(unit, root=tree).met == "catalogued"


def test_a_pytest_function_in_e2e_is_evidence_too(tree):
    spec = _spec(maturity="verified",
                 evidence=("cat/x.json", "tests/e2e/e2e.py::test_pytest_style"))
    assert plugin_conformance(spec, root=tree).ok


def test_evidence_that_does_not_resolve_fails_even_when_the_tier_is_met(tree):
    """Stale evidence is how a tier silently stops being true."""
    spec = _spec(maturity="probed", pinned=(), search_aliases={},
                 evidence=("cat/x.json", "tests/e2e/e2e.py::a check someone renamed"))
    report = plugin_conformance(spec, root=tree)
    assert report.met == "catalogued" and not report.ok
    assert "a check someone renamed" in report.broken[0]


def test_per_user_needs_identity_support_and_a_matches_caller_assertion(tree):
    spec = _spec(
        maturity="per-user",
        identity=IdentitySupport(modes=("bearer",), target="header"),
        evidence=("cat/x.json", "tests/e2e/e2e.py::the caller is themselves"),
    )
    assert plugin_conformance(spec, root=tree).ok
    no_identity = replace(spec, identity=IdentitySupport())
    assert plugin_conformance(no_identity, root=tree).met == "verified"
    no_assertion = replace(spec, evidence=("cat/x.json", "tests/e2e/e2e.py::it attaches for real"))
    report = plugin_conformance(no_assertion, root=tree)
    assert report.met == "verified" and "matches_caller" in report.problems[0]


def test_an_unknown_tier_is_not_ok(tree):
    report = plugin_conformance(_spec(maturity="gold"), root=tree)
    assert not report.ok


def test_tiers_are_ordered_lowest_first():
    assert MATURITY_TIERS == ("declared", "probed", "catalogued", "verified", "per-user")


def test_register_refuses_an_unknown_tier():
    async def build(ctx): ...

    with pytest.raises(UsageError, match="maturity must be one of"):
        register(_spec(name="maturity-test-gold", maturity="gold"), build)
    assert "maturity-test-gold" not in PLUGINS


# --- runtime display and lint ----------------------------------------------


def test_an_out_of_tree_claim_without_resolvable_evidence_is_capped_at_probed():
    async def build(ctx): ...

    build.__module__ = "acme_not_installed.crm"
    plugin = Plugin(spec=_spec(maturity="verified", evidence=("tests/e2e/x.py::y",)), build=build)
    tier, note = displayed_tier(plugin)
    assert tier == "probed"
    assert "claims 'verified'" in note


def test_an_out_of_tree_claim_at_or_below_probed_is_shown_as_is():
    async def build(ctx): ...

    build.__module__ = "acme_not_installed.crm"
    plugin = Plugin(spec=_spec(maturity="probed"), build=build)
    assert displayed_tier(plugin) == ("probed", None)


def test_an_in_tree_plugin_shows_its_declared_tier():
    assert displayed_tier(PLUGINS["plane-http"]) == ("per-user", None)


def test_lint_warning_names_what_would_raise_a_declared_plugin():
    generic = lint_warning("wiki", "mcp-http", PLUGINS["mcp-http"].spec)
    assert "'declared'" in generic and "health --deep" in generic and "catalogue" in generic
    curated = lint_warning("cal", "x", _spec(evidence=()))
    assert "recorded catalogue" in curated
    assert lint_warning("plane", "plane", PLUGINS["plane"].spec) is None

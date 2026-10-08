"""`attach --config k=v,...` coerces each value to the plugin's declared type.

Before, every value arrived as a string, so `max_results=50` failed the
plugin's own type check ("must be int, got str") — the CLI could not attach any
plugin with a non-string config key at all.
"""

import pytest

from beherouter.cli.app import parse_config
from beherouter.errors import UsageError
from beherouter.plugins.spec import ConfigField, PluginSpec

SPEC = PluginSpec(
    name="typed",
    summary="typed config",
    backing="http",
    config=(
        ConfigField("url", str),
        ConfigField("n", int),
        ConfigField("ratio", float),
        ConfigField("on", bool),
        ConfigField("include", list),
    ),
)


def test_an_empty_string_is_no_config():
    assert parse_config(SPEC, "") == {}


def test_each_value_takes_its_declared_type():
    got = parse_config(SPEC, "url=http://x:1/mcp,n=50,ratio=0.5,on=true")
    assert got == {"url": "http://x:1/mcp", "n": 50, "ratio": 0.5, "on": True}
    assert type(got["n"]) is int


@pytest.mark.parametrize("raw, want", [("true", True), ("FALSE", False), ("True", True)])
def test_bool_accepts_true_and_false_only_case_insensitively(raw, want):
    assert parse_config(SPEC, f"on={raw}") == {"on": want}


@pytest.mark.parametrize("raw", ["yes", "1", "on", ""])
def test_a_bool_that_is_not_true_or_false_is_refused(raw):
    # "yes"/"1" are refused on purpose: TOML itself has exactly two booleans,
    # and the CLI should not accept a spelling the registry file would not.
    with pytest.raises(UsageError, match=r"'on'.*bool"):
        parse_config(SPEC, f"on={raw}")


@pytest.mark.parametrize(
    "pair, key, type_", [("n=fifty", "n", "int"), ("ratio=x", "ratio", "float")]
)
def test_an_uncoercible_number_names_key_and_type(pair, key, type_):
    with pytest.raises(UsageError, match=rf"'{key}'.*{type_}"):
        parse_config(SPEC, pair)


def test_a_list_is_one_pair_per_item():
    """`,` already separates pairs, so a list cannot also use it: repeating the
    key is the only spelling that needs no second escape character."""
    assert parse_config(SPEC, "include=getPet,include=listPets") == {
        "include": ["getPet", "listPets"]
    }


def test_a_single_list_item_is_still_a_list():
    assert parse_config(SPEC, "include=*") == {"include": ["*"]}


def test_repeating_a_scalar_key_is_refused():
    with pytest.raises(UsageError, match=r"'n'.*more than once"):
        parse_config(SPEC, "n=1,n=2")


def test_a_pair_without_equals_is_refused():
    with pytest.raises(UsageError, match="key=value"):
        parse_config(SPEC, "n")


def test_an_undeclared_key_is_left_for_validate_config_to_name():
    """Unknown keys pass through as strings: `validate_config` already refuses
    them with the list of allowed keys, which is the better message."""
    assert parse_config(SPEC, "bogus=1") == {"bogus": "1"}


def test_whitespace_around_keys_and_values_is_stripped():
    assert parse_config(SPEC, " n = 7 , on = false ") == {"n": 7, "on": False}

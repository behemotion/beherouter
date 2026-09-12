import pytest

from beherouter.errors import UsageError
from beherouter.plugins.spec import ConfigField, PluginSpec
from beherouter.plugins.validate import validate_config

SPEC = PluginSpec(
    name="demo",
    summary="x",
    backing="http",
    config=(
        ConfigField(name="base_url", type=str, default="http://d:1/"),
        ConfigField(name="slug", type=str, required=True),
        ConfigField(name="limit", type=int, default=50),
        ConfigField(name="strict", type=bool, default=False),
    ),
)


def test_defaults_are_applied():
    out = validate_config("s", SPEC, {"slug": "homelab"})
    assert out == {"base_url": "http://d:1/", "slug": "homelab", "limit": 50, "strict": False}


def test_supplied_value_overrides_default():
    assert validate_config("s", SPEC, {"slug": "h", "limit": 5})["limit"] == 5


def test_missing_required_key_raises():
    with pytest.raises(UsageError, match="slug"):
        validate_config("s", SPEC, {})


def test_unknown_key_raises_and_names_the_allowed_ones():
    with pytest.raises(UsageError, match="base_url"):
        validate_config("s", SPEC, {"slug": "h", "nope": 1})


def test_wrong_type_raises():
    with pytest.raises(UsageError, match="limit"):
        validate_config("s", SPEC, {"slug": "h", "limit": "fifty"})


def test_bool_is_not_accepted_as_int():
    """TOML has real booleans; silently coercing True to 1 hides a typo."""
    with pytest.raises(UsageError, match="limit"):
        validate_config("s", SPEC, {"slug": "h", "limit": True})


def test_none_config_is_the_same_as_empty():
    with pytest.raises(UsageError, match="slug"):
        validate_config("s", SPEC, None)


def test_plugin_with_no_config_rejects_any_key():
    bare = PluginSpec(name="bare", summary="x", backing="http")
    with pytest.raises(UsageError, match="takes no config"):
        validate_config("s", bare, {"a": 1})

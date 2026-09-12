import pytest

from beherouter.errors import UsageError
from beherouter.registry import (
    RegistryEntry,
    load_registry,
    save_registry,
    validate_entry,
)


def test_plugin_is_the_only_required_key(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[office]\nplugin = "office-mcp"\n')
    assert load_registry(p)["office"].plugin == "office-mcp"


def test_kind_is_no_longer_an_allowed_key(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[x]\nplugin = "office-mcp"\nkind = "mcp"\n')
    with pytest.raises(UsageError, match="kind"):
        load_registry(p)


def test_unknown_plugin_is_rejected():
    with pytest.raises(UsageError, match="unknown plugin"):
        validate_entry(RegistryEntry(name="x", plugin="nope"))


def test_entry_config_is_validated_against_the_plugin():
    with pytest.raises(UsageError, match="workspace_slug"):
        validate_entry(RegistryEntry(name="plane", plugin="plane"))


def test_plugin_validator_runs():
    with pytest.raises(UsageError, match="underscore"):
        validate_entry(
            RegistryEntry(
                name="plane",
                plugin="plane",
                config={"base_url": "http://plane_api_1:8000", "workspace_slug": "h"},
            )
        )


def test_undeclared_credential_is_rejected():
    """office-mcp has no app-level auth; a token here would be a lie."""
    with pytest.raises(UsageError, match="declares no credential"):
        validate_entry(
            RegistryEntry(name="office", plugin="office-mcp", env={"api_key": "${T}"})
        )


def test_missing_declared_credential_is_rejected():
    with pytest.raises(UsageError, match="api_key"):
        validate_entry(
            RegistryEntry(
                name="plane", plugin="plane", config={"workspace_slug": "homelab"}
            )
        )


def test_validate_accepts_the_two_live_entries():
    validate_entry(RegistryEntry(name="office", plugin="office-mcp"))
    validate_entry(
        RegistryEntry(
            name="plane",
            plugin="plane",
            config={"workspace_slug": "homelab"},
            env={"api_key": "${BEHEROUTER_PLANE_TOKEN}"},
        )
    )


def test_roundtrip(tmp_path):
    p = tmp_path / "registry.toml"
    entries = {
        "office": RegistryEntry(name="office", plugin="office-mcp"),
        "plane": RegistryEntry(
            name="plane",
            plugin="plane",
            config={"workspace_slug": "homelab"},
            env={"api_key": "${BEHEROUTER_PLANE_TOKEN}"},
        ),
    }
    save_registry(p, entries)
    loaded = load_registry(p)
    assert loaded["office"].plugin == "office-mcp"
    assert loaded["plane"].config == {"workspace_slug": "homelab"}
    assert loaded["plane"].env == {"api_key": "${BEHEROUTER_PLANE_TOKEN}"}


def test_roundtrip_preserves_pinned_list(tmp_path):
    p = tmp_path / "registry.toml"
    save_registry(p, {"x": RegistryEntry(name="x", plugin="office-mcp", pinned=["a", "b"])})
    assert load_registry(p)["x"].pinned == ["a", "b"]


def test_roundtrip_preserves_probe(tmp_path):
    """`probe` names the one backend tool a deep health check may call. It is an
    OVERRIDE of the plugin's tested default, so it must survive a round trip."""
    p = tmp_path / "registry.toml"
    save_registry(
        p, {"g": RegistryEntry(name="g", plugin="office-mcp", probe="job_status")}
    )
    assert load_registry(p)["g"].probe == "job_status"


def test_roundtrip_survives_quotes_and_backslashes(tmp_path):
    """Config values are arbitrary paths — hand-rolled TOML must escape them."""
    p = tmp_path / "registry.toml"
    nasty = 'py "C:\\Program Files\\t.py"'
    save_registry(p, {"x": RegistryEntry(name="x", plugin="_test-cli", config={"cmd": nasty})})
    assert load_registry(p)["x"].config["cmd"] == nasty


def test_save_round_trips_typed_config(tmp_path):
    """A plugin's config carries typed scalars: writing 50 as "50" would fail the
    plugin's own type check on the next load."""
    p = tmp_path / "r.toml"
    save_registry(
        p,
        {"x": RegistryEntry(name="x", plugin="office-mcp", config={"n": 5, "on": True, "s": "t"})},
    )
    cfg = load_registry(p)["x"].config
    assert cfg == {"n": 5, "on": True, "s": "t"}
    assert cfg["n"] is not True  # an int must not round-trip as a bool


def test_missing_file_returns_empty(tmp_path):
    assert load_registry(tmp_path / "nope.toml") == {}


def test_unknown_key_raises_usage_error(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text('[x]\nplugin = "office-mcp"\nbogus = "y"\n')
    with pytest.raises(UsageError):
        load_registry(p)


def test_catalogue_ttl_ms_is_an_accepted_registry_key(tmp_path):
    from beherouter.registry import load_registry

    p = tmp_path / "r.toml"
    p.write_text('[plane]\nplugin = "plane"\ncatalogue_ttl_ms = 60000\n')
    assert load_registry(p)["plane"].catalogue_ttl_ms == 60000


def test_catalogue_ttl_ms_defaults_to_none(tmp_path):
    from beherouter.registry import load_registry

    p = tmp_path / "r.toml"
    p.write_text('[plane]\nplugin = "plane"\n')
    assert load_registry(p)["plane"].catalogue_ttl_ms is None


def test_a_negative_catalogue_ttl_is_refused():
    from beherouter.errors import UsageError
    from beherouter.registry import RegistryEntry, validate_entry

    with pytest.raises(UsageError, match="catalogue_ttl_ms"):
        validate_entry(
            RegistryEntry(name="office", plugin="office-mcp", catalogue_ttl_ms=-1)
        )


def test_a_pin_list_written_as_a_string_is_refused():
    """The pin-check AMPLIFIES this gap rather than covering it.

    `pinned = "search"` is a plausible hand-edit (TOML has no array coercion).
    It passes the dataclass, and `health --deep` then iterates the string
    CHARACTER BY CHARACTER and reports `pinned_missing: ["a","c","e","h","r","s"]`
    — a verdict that looks like real drift and names tools that never existed.
    """
    from beherouter.errors import UsageError
    from beherouter.registry import RegistryEntry, validate_entry

    with pytest.raises(UsageError, match="pinned"):
        validate_entry(RegistryEntry(name="office", plugin="office-mcp", pinned="search"))


def test_a_pin_list_of_non_strings_is_refused():
    from beherouter.errors import UsageError
    from beherouter.registry import RegistryEntry, validate_entry

    with pytest.raises(UsageError, match="pinned"):
        validate_entry(RegistryEntry(name="office", plugin="office-mcp", pinned=[1, 2]))


def test_probe_args_that_is_not_a_table_is_refused():
    """`probe` and `probe_args` resolve as ONE unit, so a malformed `probe_args`
    does not fail loudly at lint time — it reaches the probe call."""
    from beherouter.errors import UsageError
    from beherouter.registry import RegistryEntry, validate_entry

    with pytest.raises(UsageError, match="probe_args"):
        validate_entry(
            RegistryEntry(
                name="office", plugin="office-mcp", probe="discover", probe_args="q=x"
            )
        )


def test_a_well_formed_pin_list_and_probe_args_still_validate():
    from beherouter.registry import RegistryEntry, validate_entry

    validate_entry(
        RegistryEntry(
            name="office",
            plugin="office-mcp",
            pinned=["discover", "invoke"],
            probe="discover",
            probe_args={"query": "x"},
        )
    )

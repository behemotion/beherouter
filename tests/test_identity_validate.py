import pytest

from beherouter.errors import UsageError
from beherouter.identity import policy_from_entry, validate_identity
from beherouter.plugins.spec import EnvVar, IdentitySupport, PluginSpec
from beherouter.registry import RegistryEntry

HTTP = PluginSpec(
    name="demo-http",
    summary="demo",
    backing="http",
    identity=IdentitySupport(modes=("bearer", "claims", "client"), target="header"),
)
STDIO = PluginSpec(
    name="demo-stdio",
    summary="demo",
    backing="stdio",
    identity=IdentitySupport(modes=("bearer",), target="header"),
)
PLAIN = PluginSpec(name="demo-plain", summary="demo", backing="http")
NATIVE = PluginSpec(
    name="demo-native",
    summary="demo",
    backing="native",
    env=(EnvVar(name="refresh_token"),),
    identity=IdentitySupport(
        modes=("lookup",), target="credential", accepts=("refresh_token",)
    ),
)


def test_no_identity_table_is_valid():
    validate_identity("s", HTTP, None)
    validate_identity("s", PLAIN, None)


def test_a_plugin_without_identity_support_cannot_be_configured():
    with pytest.raises(UsageError, match="declares no identity support"):
        validate_identity("s", PLAIN, {"mode": "bearer"})


def test_a_stdio_backing_is_refused():
    with pytest.raises(UsageError, match="stdio"):
        validate_identity("s", STDIO, {"mode": "bearer"})


def test_an_unknown_mode_names_the_known_ones():
    with pytest.raises(UsageError, match="bearer"):
        validate_identity("s", HTTP, {"mode": "oauth-magic"})


def test_a_mode_the_plugin_does_not_support_is_refused():
    with pytest.raises(UsageError, match="supports identity mode"):
        validate_identity("s", NATIVE, {"mode": "bearer"})


def test_an_unknown_identity_key_is_refused():
    with pytest.raises(UsageError, match="typo"):
        validate_identity("s", HTTP, {"mode": "bearer", "typo": 1})


def test_claims_mode_requires_a_non_empty_map():
    with pytest.raises(UsageError, match="requires a non-empty"):
        validate_identity("s", HTTP, {"mode": "claims"})
    with pytest.raises(UsageError, match="requires a non-empty"):
        validate_identity("s", HTTP, {"mode": "claims", "map": {}})


def test_bearer_mode_refuses_a_map():
    with pytest.raises(UsageError, match="not a map"):
        validate_identity("s", HTTP, {"mode": "bearer", "map": {"a": "b"}})


def test_a_target_key_outside_accepts_is_refused():
    with pytest.raises(UsageError, match="not an identity target"):
        validate_identity(
            "s",
            NATIVE,
            {
                "mode": "lookup",
                "key": "email",
                "path": "/m.toml",
                "map": {"client_secret": "client_secret"},
            },
        )


def test_a_valid_native_lookup_entry_passes():
    validate_identity(
        "s",
        NATIVE,
        {
            "mode": "lookup",
            "key": "email",
            "path": "/m.toml",
            "map": {"refresh_token": "refresh_token"},
        },
    )


def test_lookup_without_a_path_or_env_var_is_refused(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_IDENTITY_MAP", raising=False)
    with pytest.raises(UsageError, match="BEHEROUTER_IDENTITY_MAP"):
        validate_identity(
            "s", NATIVE, {"mode": "lookup", "map": {"refresh_token": "refresh_token"}}
        )


def test_policy_from_entry_carries_the_declared_target_and_defaults():
    entry = RegistryEntry(name="plane", plugin="demo-http", identity={"mode": "bearer"})
    policy = policy_from_entry(entry, HTTP)
    assert policy.enabled and policy.target == "header"
    assert policy.header == "authorization" and policy.prefix == "Bearer "
    assert policy.surface == "plane"


def test_policy_from_entry_is_disabled_without_a_table():
    policy = policy_from_entry(RegistryEntry(name="office", plugin="demo-http"), HTTP)
    assert policy.enabled is False


def test_registry_entry_rejects_a_bad_identity_table():
    """Through the real registry path, on a plugin that can never be per-user.

    `plane` is a stdio backing: a subprocess environment is fixed at spawn and
    reused across callers, so it declares no IdentitySupport and this entry
    stays refused however the plugin table grows.
    """
    from beherouter.registry import validate_entry

    entry = RegistryEntry(
        name="s",
        plugin="plane",
        config={"workspace_slug": "homelab"},
        env={"api_key": "x"},
        identity={"mode": "bearer"},
    )
    with pytest.raises(UsageError, match="identity"):
        validate_entry(entry)

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


def test_role_gated_counts_a_tool_gate():
    from beherouter.identity import gates_on_caller, role_gated, tool_roles
    from beherouter.registry import RegistryEntry

    e = RegistryEntry(
        name="s", plugin="office-mcp",
        authz={"tools": {"convert": {"require_roles": ["w", "x"]}}},
    )
    assert tool_roles(e) == {"convert": ("w", "x")}
    assert role_gated(e) and gates_on_caller(e)
    plain = RegistryEntry(name="s", plugin="office-mcp")
    assert tool_roles(plain) == {} and not role_gated(plain)


def test_check_roles_names_what_it_gates():
    import pytest

    from beherouter.errors import AuthError
    from beherouter.identity import RequestIdentity, check_roles

    req = RequestIdentity(
        shared=False, subject="alice", claims={"roles": ["a"]}, raw_token="t"
    )
    with pytest.raises(AuthError, match="tool 'w' on surface 's': it requires role"):
        check_roles("tool 'w' on surface 's'", ("a", "b"), "roles", req)
    check_roles("tool 'w' on surface 's'", ("a",), "roles", req)


def test_an_identity_table_that_is_not_a_table_is_refused():
    with pytest.raises(UsageError, match="must be a table"):
        validate_identity("s", HTTP, ["bearer"])


def test_mode_none_is_valid_alone_and_refuses_stray_keys():
    validate_identity("s", PLAIN, {"mode": "none"})
    with pytest.raises(UsageError, match="silently ignored"):
        validate_identity("s", HTTP, {"mode": "none", "header": "x"})


def test_a_plugin_declaring_an_unknown_target_is_refused():
    spec = PluginSpec(
        name="bad", summary="d", backing="http",
        identity=IdentitySupport(modes=("bearer",), target="carrier-pigeon"),
    )
    with pytest.raises(UsageError, match=r"IdentitySupport\.target"):
        validate_identity("s", spec, {"mode": "bearer"})


def test_a_credential_target_without_accepts_is_refused():
    spec = PluginSpec(
        name="bad", summary="d", backing="native",
        identity=IdentitySupport(modes=("lookup",), target="credential"),
    )
    with pytest.raises(UsageError, match="requires 'accepts'"):
        validate_identity("s", spec, {"mode": "lookup", "path": "/m", "map": {"a": "b"}})


@pytest.mark.parametrize("source", ["", 3])
def test_a_map_source_must_be_a_non_empty_string(source):
    with pytest.raises(UsageError, match="must name a non-empty source"):
        validate_identity("s", HTTP, {"mode": "claims", "map": {"x-user": source}})


@pytest.mark.parametrize("key", ["", 5])
def test_lookup_key_must_name_a_claim(key):
    with pytest.raises(UsageError, match="'key' must name a claim"):
        validate_identity(
            "s", NATIVE,
            {"mode": "lookup", "key": key, "path": "/m",
             "map": {"refresh_token": "refresh_token"}},
        )


def test_an_authz_table_that_is_not_a_table_is_refused():
    from beherouter.identity import validate_authz

    with pytest.raises(UsageError, match=r"\[authz\] must be a table"):
        validate_authz("s", "admins")


def test_policy_from_entry_treats_mode_none_as_no_mode():
    entry = RegistryEntry(name="s", plugin="demo-http", identity={"mode": "none"})
    policy = policy_from_entry(entry, HTTP)
    assert policy.mode == "" and policy.enabled is False


def test_identity_report_for_each_kind_of_surface(monkeypatch):
    from beherouter.identity import IdentityPolicy, identity_report
    from beherouter.tokenexchange import ExchangeConfig

    assert identity_report(IdentityPolicy(surface="s")) == {"mode": "none"}
    assert identity_report(
        IdentityPolicy(surface="s", require_roles=("r",), audiences=("a",))
    ) == {"mode": "none", "require_roles": ["r"], "audience": ["a"], "hide_tools": True}
    gated = identity_report(
        IdentityPolicy(surface="s", mode="bearer", target="header", require_roles=("r",))
    )
    assert gated == {
        "mode": "bearer", "probe_scope": "deployment-credential",
        "require_roles": ["r"], "hide_tools": True,
    }
    ex = ExchangeConfig(
        token_url="https://idp.test/t", client_id="c", client_secret="${UNSET_X}",
        resource=("https://crm.test/",), scope="read write",
    )
    report = identity_report(
        IdentityPolicy(surface="s", mode="exchange", target="header", exchange=ex)
    )
    assert report["exchange"] == {
        "token_url": "https://idp.test/t", "resource": ["https://crm.test/"],
        "scope": "read write", "client_auth": "client_secret_basic",
        "client_secret": "unset",
    }
    monkeypatch.delenv("BEHEROUTER_IDENTITY_MAP", raising=False)
    lookup = identity_report(
        IdentityPolicy(surface="s", mode="lookup", target="header", map={"k": "v"})
    )
    assert lookup["map"]["state"] == "missing"
    assert "BEHEROUTER_IDENTITY_MAP" in lookup["map"]["error"]

"""Per-surface role gating: `[surface.authz] require_roles`.

SEPARATE FROM IDENTITY ON PURPOSE. A surface may want a role gate while
forwarding nothing — role-gating `office` without changing one byte of what
office receives — so `authz` is its own registry table with its own validator,
and it needs no IdentitySupport from the plugin. That is also why it works on a
`stdio` backing, which can never carry an identity.

⚠️ THIS CARRIES NO SECURITY WEIGHT. The backend's own verification is the
control; this turns an opaque backend 401 into "you do not have access to this
surface" at the edge. It is ergonomics and diagnosability, not authorization.
"""

import pytest

from beherouter.errors import AuthError, UsageError
from beherouter.identity import (
    IdentityPolicy,
    RequestIdentity,
    claim_at,
    policy_from_entry,
    validate_authz,
)
from beherouter.plugins.spec import IdentitySupport, PluginSpec
from beherouter.registry import RegistryEntry

HTTP = PluginSpec(
    name="demo-http",
    summary="demo",
    backing="http",
    identity=IdentitySupport(modes=("bearer",), target="header"),
)
STDIO = PluginSpec(name="demo-stdio", summary="demo", backing="stdio")


def _user(claims=None) -> RequestIdentity:
    return RequestIdentity(
        shared=False,
        subject="alice",
        claims=claims if claims is not None else {"sub": "alice"},
        raw_token="jwt-token",
    )


def _gate(require, claim="realm_access.roles", mode="") -> IdentityPolicy:
    return IdentityPolicy(
        surface="plane",
        mode=mode,
        target="header" if mode else "",
        require_roles=tuple(require),
        roles_claim=claim,
    )


# --- the dotted claim path --------------------------------------------------


def test_claim_at_walks_a_dotted_path():
    claims = {"realm_access": {"roles": ["a", "b"]}}
    assert claim_at(claims, "realm_access.roles") == ["a", "b"]


def test_claim_at_reads_a_top_level_claim():
    assert claim_at({"roles": ["a"]}, "roles") == ["a"]


def test_claim_at_returns_none_for_an_absent_path():
    assert claim_at({"realm_access": {}}, "realm_access.roles") is None
    assert claim_at({}, "groups") is None
    # a non-mapping half way down is absent, not a crash
    assert claim_at({"realm_access": "nope"}, "realm_access.roles") is None


# --- the gate ---------------------------------------------------------------


def test_a_role_gate_alone_enables_the_policy():
    """An authz-only surface needs a verified caller even with no mode."""
    assert _gate(["ai-plane-access"]).enabled is True
    assert IdentityPolicy(surface="plane").enabled is False


def test_a_caller_holding_the_role_passes_and_forwards_nothing():
    policy = _gate(["ai-plane-access"])
    req = _user({"sub": "alice", "realm_access": {"roles": ["ai-plane-access"]}})
    # No mode: nothing is materialised, and the dispatch in surface.py already
    # handles a None identity.
    assert policy.authorise(req) is None


def test_a_caller_without_the_role_is_refused_by_name():
    policy = _gate(["ai-plane-access"])
    req = _user({"sub": "alice", "realm_access": {"roles": ["something-else"]}})
    with pytest.raises(AuthError, match="ai-plane-access"):
        policy.authorise(req)


def test_a_caller_missing_the_claim_entirely_is_refused():
    with pytest.raises(AuthError, match="realm_access.roles"):
        _gate(["ai-plane-access"]).authorise(_user())


def test_every_required_role_must_be_held():
    policy = _gate(["a", "b"])
    req = _user({"sub": "alice", "realm_access": {"roles": ["a"]}})
    with pytest.raises(AuthError, match="b"):
        policy.authorise(req)
    ok = _user({"sub": "alice", "realm_access": {"roles": ["a", "b", "c"]}})
    assert policy.authorise(ok) is None


def test_a_space_delimited_claim_is_accepted():
    """Entra and some proxies emit roles as one space-delimited string."""
    policy = _gate(["a"], claim="roles")
    assert policy.authorise(_user({"sub": "alice", "roles": "a b"})) is None


def test_a_shared_token_caller_is_refused_by_a_role_gate():
    with pytest.raises(AuthError, match="shared gateway token"):
        _gate(["a"]).authorise(RequestIdentity(shared=True, subject=None))


def test_a_role_gate_without_a_configured_claim_path_fails_closed():
    with pytest.raises(UsageError, match="BEHEROUTER_OIDC_ROLES_CLAIM"):
        _gate(["a"], claim="").authorise(_user())


def test_a_gate_and_a_mode_compose():
    """The role is checked, and then the identity is still materialised."""
    policy = _gate(["a"], mode="bearer")
    req = _user({"sub": "alice", "realm_access": {"roles": ["a"]}})
    ident = policy.authorise(req)
    assert ident is not None and ident.headers == {"authorization": "Bearer jwt-token"}


def test_a_mode_is_not_reached_when_the_role_check_refuses():
    policy = _gate(["a"], mode="bearer")
    req = _user({"sub": "alice", "realm_access": {"roles": []}})
    with pytest.raises(AuthError, match="'a'"):
        policy.authorise(req)


# --- validation -------------------------------------------------------------


def test_no_authz_table_is_valid():
    validate_authz("s", None)
    validate_authz("s", {})


def test_require_roles_must_be_a_non_empty_list_of_strings():
    for bad in ({"require_roles": []}, {"require_roles": "a"}, {"require_roles": [1]}):
        with pytest.raises(UsageError, match="require_roles"):
            validate_authz("s", bad)


def test_an_unknown_authz_key_is_refused():
    with pytest.raises(UsageError, match="typo"):
        validate_authz("s", {"require_roles": ["a"], "typo": 1})


def test_a_valid_authz_table_passes():
    validate_authz("s", {"require_roles": ["ai-plane-access"]})


def test_a_role_gate_is_allowed_on_a_stdio_backing(monkeypatch):
    """stdio cannot carry an identity, but it can be gated: nothing is forwarded."""
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "realm_access.roles")
    entry = RegistryEntry(
        name="plane", plugin="demo-stdio", authz={"require_roles": ["a"]}
    )
    policy = policy_from_entry(entry, STDIO)
    assert policy.enabled and policy.mode == "" and policy.require_roles == ("a",)


def test_policy_from_entry_reads_the_claim_path_from_the_environment(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "groups")
    entry = RegistryEntry(name="s", plugin="demo-http", authz={"require_roles": ["a"]})
    assert policy_from_entry(entry, HTTP).roles_claim == "groups"


def test_policy_from_entry_without_authz_has_no_roles():
    entry = RegistryEntry(name="s", plugin="demo-http", identity={"mode": "bearer"})
    assert policy_from_entry(entry, HTTP).require_roles == ()


# --- the gate on its own, without materialising -----------------------------


def test_the_gate_checks_roles_without_materialising():
    """`gate` is the refusal half alone: no credential is ever resolved.

    The read-only meta-tools need the gate WITHOUT the material — they call the
    backend with the deployment credential by design — so a `lookup` surface
    whose map is absent must still answer a search for a caller who holds the
    role. Materialising here would turn a missing map into a refusal to
    enumerate.
    """
    policy = IdentityPolicy(
        surface="gcal",
        mode="lookup",
        target="credential",
        map={"refresh_token": "refresh_token"},
        path="/nonexistent/identity-map.toml",
        require_roles=("ai-calendar-access",),
        roles_claim="realm_access.roles",
    )
    claims = {"sub": "alice", "realm_access": {"roles": ["ai-calendar-access"]}}
    assert policy.gate(_user(claims)) is None


def test_the_gate_refuses_a_caller_without_the_role():
    policy = _gate(["ai-plane-access"])
    with pytest.raises(AuthError, match="do not have access"):
        policy.gate(_user({"sub": "alice", "realm_access": {"roles": ["other"]}}))


def test_the_gate_refuses_a_shared_token_caller():
    policy = _gate(["ai-plane-access"])
    with pytest.raises(AuthError, match="requires a verified user"):
        policy.gate(RequestIdentity(shared=True, subject=None))


# --- per-surface audience ------------------------------------------------------
#
# `BEHEROUTER_OIDC_AUDIENCE` is gateway-wide, so with several teams' surfaces on
# one gateway every surface accepted every team's tokens. `[surface.authz]
# audience` narrows a surface to tokens addressed to it, on top of (never
# instead of) the gateway-wide verification.


def _aud_gate(audiences) -> IdentityPolicy:
    return IdentityPolicy(surface="plane", audiences=tuple(audiences))


def test_a_token_addressed_to_the_surface_passes():
    _aud_gate(["plane-mcp"]).gate(_user({"sub": "alice", "aud": ["beherouter", "plane-mcp"]}))
    _aud_gate(["plane-mcp"]).gate(_user({"sub": "alice", "aud": "plane-mcp"}))


def test_any_one_listed_audience_is_enough():
    _aud_gate(["plane-mcp", "plane"]).gate(_user({"sub": "alice", "aud": "plane"}))


def test_a_token_for_another_surface_is_refused_naming_the_expected_audience():
    with pytest.raises(AuthError, match="'plane-mcp'") as e:
        _aud_gate(["plane-mcp"]).gate(_user({"sub": "alice", "aud": ["wiki-mcp"]}))
    assert "wiki-mcp" not in str(e.value)  # never echo the token's own claims


def test_a_token_with_no_aud_is_refused():
    with pytest.raises(AuthError):
        _aud_gate(["plane-mcp"]).gate(_user({"sub": "alice"}))


def test_an_audience_gate_alone_enables_the_policy_and_refuses_shared_callers():
    policy = _aud_gate(["plane-mcp"])
    assert policy.enabled
    with pytest.raises(AuthError):
        policy.gate(RequestIdentity(shared=True))


@pytest.mark.parametrize("value", ["plane-mcp", ["plane-mcp", "plane"]])
def test_audience_validates_as_a_string_or_a_list(value):
    validate_authz("plane", {"audience": value})


@pytest.mark.parametrize("value", ["", [], [""], [1], {"a": 1}])
def test_a_malformed_audience_is_refused_offline(value):
    with pytest.raises(UsageError, match="audience"):
        validate_authz("plane", {"audience": value})


def test_policy_from_entry_carries_the_audience():
    entry = RegistryEntry(
        name="plane", plugin="demo-http", authz={"audience": "plane-mcp"}
    )
    assert policy_from_entry(entry, HTTP).audiences == ("plane-mcp",)


async def test_boot_refuses_an_audience_gate_on_a_shared_gateway(monkeypatch):
    from beherouter.gateway import build_gateway_app

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    entry = RegistryEntry(name="office", plugin="office-mcp", authz={"audience": "x"})
    with pytest.raises(UsageError, match="shared"):
        await build_gateway_app({"office": entry})


def test_lint_refuses_an_audience_gate_on_a_shared_gateway(tmp_path, monkeypatch):
    from beherouter.cli.app import registry_lint

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    path = tmp_path / "registry.toml"
    path.write_text('[office]\nplugin = "office-mcp"\n  [office.authz]\n  audience = "x"\n')
    with pytest.raises(UsageError, match="shared"):
        registry_lint(path=str(path))


def test_an_audience_gate_needs_no_roles_claim(tmp_path, monkeypatch):
    """Unlike require_roles, an audience lives in a standard claim."""
    from beherouter.cli.app import registry_lint

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.delenv("BEHEROUTER_OIDC_ROLES_CLAIM", raising=False)
    path = tmp_path / "registry.toml"
    path.write_text('[office]\nplugin = "office-mcp"\n  [office.authz]\n  audience = "x"\n')
    registry_lint(path=str(path))

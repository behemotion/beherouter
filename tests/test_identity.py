import pytest

from beherouter.errors import AuthError
from beherouter.identity import (
    CallIdentity,
    IdentityPolicy,
    RequestIdentity,
    material_key,
)


def _user(**kw) -> RequestIdentity:
    base = {
        "shared": False,
        "subject": "alice",
        "claims": {"sub": "alice", "email": "alice@example.test"},
        "raw_token": "jwt-token",
        "headers": {},
    }
    return RequestIdentity(**{**base, **kw})


def test_a_surface_without_a_mode_is_disabled():
    assert IdentityPolicy(surface="office").enabled is False


def test_bearer_forwards_the_callers_token_as_a_header():
    p = IdentityPolicy(surface="plane", mode="bearer", target="header")
    ident = p.materialise(_user())
    assert ident.headers == {"authorization": "Bearer jwt-token"}
    assert ident.env == {} and ident.credentials == {}
    assert ident.subject == "alice"


def test_bearer_honours_a_configured_header_and_empty_prefix():
    p = IdentityPolicy(
        surface="plane",
        mode="bearer",
        target="header",
        header="x-user-token",
        prefix="",
    )
    assert p.materialise(_user()).headers == {"x-user-token": "jwt-token"}


def test_bearer_without_a_token_refuses():
    p = IdentityPolicy(surface="plane", mode="bearer", target="header")
    with pytest.raises(AuthError, match="carries none"):
        p.materialise(_user(raw_token=None))


def test_claims_map_onto_the_configured_target_keys():
    p = IdentityPolicy(
        surface="wiki",
        mode="claims",
        target="header",
        map={"x-remote-user": "email", "x-remote-id": "sub"},
    )
    assert p.materialise(_user()).headers == {
        "x-remote-user": "alice@example.test",
        "x-remote-id": "alice",
    }


def test_claims_reach_the_env_slot_for_a_cli_backing():
    p = IdentityPolicy(
        surface="tool", mode="claims", target="env", map={"REMOTE_USER": "email"}
    )
    ident = p.materialise(_user())
    assert ident.env == {"REMOTE_USER": "alice@example.test"}
    assert ident.headers == {}


def test_a_missing_claim_refuses_and_sends_nothing_partial():
    p = IdentityPolicy(
        surface="wiki",
        mode="claims",
        target="header",
        map={"x-remote-user": "email", "x-employee-id": "employee_id"},
    )
    with pytest.raises(AuthError, match="employee_id"):
        p.materialise(_user())


def test_client_mode_reads_only_the_named_client_headers():
    p = IdentityPolicy(
        surface="plane",
        mode="client",
        target="header",
        map={"x-api-key": "x-user-api-key", "x-workspace-slug": "x-user-workspace"},
    )
    req = _user(
        headers={
            "x-user-api-key": "pat-1",
            "x-user-workspace": "acme",
            "x-smuggled": "nope",
        }
    )
    assert p.materialise(req).headers == {
        "x-api-key": "pat-1",
        "x-workspace-slug": "acme",
    }


def test_client_mode_declares_which_headers_it_wants():
    p = IdentityPolicy(
        surface="plane",
        mode="client",
        target="header",
        map={"x-api-key": "x-user-api-key"},
    )
    assert p.wanted_headers == ("x-user-api-key",)
    assert IdentityPolicy(surface="p", mode="bearer", target="header").wanted_headers == ()


def test_a_missing_client_header_refuses():
    p = IdentityPolicy(
        surface="plane",
        mode="client",
        target="header",
        map={"x-api-key": "x-user-api-key"},
    )
    with pytest.raises(AuthError, match="x-user-api-key"):
        p.materialise(_user(headers={}))


def test_a_shared_token_caller_is_refused_on_a_per_user_surface():
    p = IdentityPolicy(surface="plane", mode="bearer", target="header")
    with pytest.raises(AuthError, match="shared gateway token"):
        p.materialise(RequestIdentity(shared=True, subject=None))


def test_an_unauthenticated_caller_is_refused_too():
    p = IdentityPolicy(surface="plane", mode="bearer", target="header")
    with pytest.raises(AuthError):
        p.materialise(RequestIdentity(shared=False, subject=None))


def test_cache_key_is_a_digest_and_never_the_secret():
    key = material_key("alice", {"x-api-key": "pat-1"})
    assert len(key) == 64 and "pat-1" not in key
    assert key == material_key("alice", {"x-api-key": "pat-1"})
    assert key != material_key("bob", {"x-api-key": "pat-1"})
    assert key != material_key("alice", {"x-api-key": "pat-2"})


def test_call_identity_defaults_are_empty():
    ident = CallIdentity(subject="alice")
    assert ident.headers == {} and ident.env == {} and ident.credentials == {}

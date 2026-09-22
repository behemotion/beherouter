import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from beherouter.auth import (
    SHARED_CLIENT_ID,
    CompositeVerifier,
    SharedTokenVerifier,
    auth_mode,
    build_verifier,
    configured_token,
    token_ok,
)


def test_token_ok_accepts_match():
    assert token_ok("secret", "secret") is True


def test_token_ok_rejects_mismatch_and_empty():
    assert token_ok("secret", "nope") is False
    assert token_ok("secret", None) is False
    assert token_ok("secret", "") is False
    assert token_ok(None, "anything") is False  # unconfigured gateway denies
    assert token_ok(None, None) is False


def test_configured_token_reads_env(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "abc")
    assert configured_token() == "abc"


def test_configured_token_empty_is_none(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "")
    assert configured_token() is None


def test_configured_token_absent_is_none(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_GATEWAY_TOKEN", raising=False)
    assert configured_token() is None


async def test_verifier_accepts_correct_token():
    v = SharedTokenVerifier("s3cret")
    tok = await v.verify_token("s3cret")
    assert tok is not None and tok.token == "s3cret"


async def test_verifier_rejects_wrong_token():
    v = SharedTokenVerifier("s3cret")
    assert await v.verify_token("wrong") is None


async def test_verifier_with_no_configured_token_rejects_everything():
    """An unconfigured gateway must fail closed, not open."""
    v = SharedTokenVerifier(None)
    assert await v.verify_token("anything") is None
    assert await v.verify_token("") is None


def test_verifier_requires_token_at_construction_when_strict(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_GATEWAY_TOKEN", raising=False)
    from beherouter.errors import UsageError

    with pytest.raises(UsageError):
        SharedTokenVerifier.from_env(strict=True)


def _jwt_pair():
    """An RSA key pair plus a verifier configured for it (no JWKS fetch)."""
    kp = RSAKeyPair.generate()
    verifier = JWTVerifier(
        public_key=kp.public_key, issuer="https://idp.test", audience="beherouter"
    )
    return kp, verifier


async def test_composite_accepts_the_shared_token():
    _kp, jwt = _jwt_pair()
    v = CompositeVerifier([SharedTokenVerifier("s3cret"), jwt])
    tok = await v.verify_token("s3cret")
    assert tok is not None and tok.client_id == SHARED_CLIENT_ID


async def test_composite_accepts_a_jwt_and_exposes_its_claims():
    kp, jwt = _jwt_pair()
    v = CompositeVerifier([SharedTokenVerifier("s3cret"), jwt])
    token = kp.create_token(
        subject="alice", issuer="https://idp.test", audience="beherouter"
    )
    tok = await v.verify_token(token)
    assert tok is not None
    assert tok.client_id != SHARED_CLIENT_ID
    assert tok.claims["sub"] == "alice"


async def test_composite_rejects_a_jwt_for_the_wrong_audience():
    kp, jwt = _jwt_pair()
    v = CompositeVerifier([SharedTokenVerifier("s3cret"), jwt])
    token = kp.create_token(
        subject="alice", issuer="https://idp.test", audience="someone-else"
    )
    assert await v.verify_token(token) is None


async def test_composite_rejects_when_every_verifier_rejects():
    _kp, jwt = _jwt_pair()
    v = CompositeVerifier([SharedTokenVerifier("s3cret"), jwt])
    assert await v.verify_token("not-a-token") is None


def test_auth_mode_defaults_to_shared(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_AUTH_MODE", raising=False)
    assert auth_mode() == "shared"


def test_auth_mode_rejects_an_unknown_mode(monkeypatch):
    from beherouter.errors import UsageError

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "oauth2")
    with pytest.raises(UsageError, match="BEHEROUTER_AUTH_MODE"):
        auth_mode()


def test_build_verifier_shared_is_the_default(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_AUTH_MODE", raising=False)
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    assert isinstance(build_verifier(), SharedTokenVerifier)


def test_build_verifier_both_needs_the_oidc_settings(monkeypatch):
    from beherouter.errors import UsageError

    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    for var in (
        "BEHEROUTER_OIDC_ISSUER",
        "BEHEROUTER_OIDC_AUDIENCE",
        "BEHEROUTER_OIDC_JWKS_URI",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(UsageError, match="BEHEROUTER_OIDC_ISSUER"):
        build_verifier()


def test_build_verifier_both_composes_two_verifiers(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("BEHEROUTER_OIDC_AUDIENCE", "beherouter")
    monkeypatch.setenv("BEHEROUTER_OIDC_JWKS_URI", "https://idp.test/jwks")
    v = build_verifier()
    assert isinstance(v, CompositeVerifier)


def test_build_verifier_oidc_needs_no_gateway_token(monkeypatch):
    """A pure-OIDC gateway has no shared secret to demand."""
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "oidc")
    monkeypatch.delenv("BEHEROUTER_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("BEHEROUTER_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("BEHEROUTER_OIDC_AUDIENCE", "beherouter")
    monkeypatch.setenv("BEHEROUTER_OIDC_JWKS_URI", "https://idp.test/jwks")
    assert isinstance(build_verifier(), JWTVerifier)


async def test_shared_verifier_stamps_the_discriminating_client_id():
    """`identity.py` tells shared from user by this exact value."""
    v = SharedTokenVerifier("s3cret")
    tok = await v.verify_token("s3cret")
    assert tok.client_id == SHARED_CLIENT_ID

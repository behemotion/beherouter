import pytest

from beherouter.auth import SharedTokenVerifier, configured_token, token_ok


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

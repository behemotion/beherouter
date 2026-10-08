"""One classification of how a call ended (spec §3)."""

import pytest

from beherouter.errors import AuthError, NotFound, Unavailable, UsageError, tag
from beherouter.outcomes import (
    EXECUTE,
    GATE,
    META_KEY,
    PREPARE,
    Outcome,
    classify,
    error_result,
)


@pytest.mark.parametrize(
    ("exc", "phase", "kind", "reason"),
    [
        (AuthError("no"), GATE, "refused", "unauthenticated"),
        (UsageError("no roles claim configured"), GATE, "unavailable", "identity_unavailable"),
        (Unavailable("map unreadable"), GATE, "unavailable", "identity_unavailable"),
        (NotFound("unknown tool"), PREPARE, "not_found", "unknown_tool"),
        (UsageError("unknown arg"), PREPARE, "tool_error", "bad_arguments"),
        (UsageError("backend rejected"), EXECUTE, "tool_error", "backend_rejected"),
        (AuthError("backend 401"), EXECUTE, "tool_error", "backend_rejected"),
        (Unavailable("backend down"), EXECUTE, "unavailable", "backend_unavailable"),
        (RuntimeError("our bug"), EXECUTE, "internal", "internal"),
        (KeyError("x"), GATE, "internal", "internal"),
    ],
)
def test_untagged_errors_classify_by_phase(exc, phase, kind, reason):
    assert classify(exc, phase) == Outcome(kind, reason)


def test_a_tag_wins_over_the_phase():
    exc = tag(AuthError("you lack it"), "missing_role", required_roles=["r"])
    assert classify(exc, EXECUTE) == Outcome("refused", "missing_role")


def test_the_first_tag_is_kept():
    exc = tag(tag(UsageError("x"), "edition_unsupported"), "backend_rejected")
    assert exc.context["reason"] == "edition_unsupported"


def test_tag_refuses_an_unknown_reason():
    with pytest.raises(ValueError):
        tag(UsageError("x"), "made_up")


def test_status_rides_on_the_outcome():
    exc = tag(UsageError("x"), "backend_rejected", status=400)
    assert classify(exc, EXECUTE) == Outcome("tool_error", "backend_rejected", 400)


def test_error_result_keeps_the_sentence_and_adds_meta():
    exc = tag(
        AuthError("you do not have access to surface 'dwh'"),
        "missing_role", required_roles=["ai-dwh-access"], missing_roles=["ai-dwh-access"],
    )
    res = error_result(exc, classify(exc, GATE)).to_mcp_result()
    assert res.isError is True
    assert res.content[0].text == "you do not have access to surface 'dwh'"
    assert res.meta == {
        META_KEY: {
            "type": "auth",
            "code": 4,
            "reason": "missing_role",
            "context": {"required_roles": ["ai-dwh-access"], "missing_roles": ["ai-dwh-access"]},
        }
    }


def test_error_result_drops_context_keys_outside_the_allow_list():
    """`context` is an allow-list: a key someone adds later cannot leak a
    caller value into a client-visible field by accident."""
    exc = UsageError("x", context={"subject": "alice", "status": 400})
    meta = error_result(exc, classify(exc, EXECUTE)).to_mcp_result().meta[META_KEY]
    assert meta["context"] == {"status": 400}


def test_an_internal_error_names_only_its_class():
    exc = RuntimeError("secret internal detail")
    res = error_result(exc, classify(exc, EXECUTE)).to_mcp_result()
    assert "secret internal detail" not in res.content[0].text
    assert "RuntimeError" in res.content[0].text
    assert res.meta[META_KEY]["reason"] == "internal"


def test_role_refusal_is_tagged_with_required_roles_only():
    from beherouter.identity import IdentityPolicy, RequestIdentity

    p = IdentityPolicy(surface="dwh", require_roles=("ai-dwh-access",), roles_claim="roles")
    req = RequestIdentity(shared=False, subject="alice", claims={"roles": ["other-role"]})
    with pytest.raises(AuthError) as err:
        p.gate(req)
    assert err.value.context == {
        "reason": "missing_role",
        "required_roles": ["ai-dwh-access"],
        "missing_roles": ["ai-dwh-access"],
    }
    assert "other-role" not in str(err.value.context)


def test_missing_roles_claim_is_a_missing_role():
    from beherouter.identity import IdentityPolicy, RequestIdentity

    p = IdentityPolicy(surface="dwh", require_roles=("r",), roles_claim="roles")
    with pytest.raises(AuthError) as err:
        p.gate(RequestIdentity(shared=False, subject="alice", claims={}))
    assert err.value.context["reason"] == "missing_role"


def test_audience_refusal_is_tagged_with_the_expected_audience():
    from beherouter.identity import IdentityPolicy, RequestIdentity

    p = IdentityPolicy(surface="dwh", audiences=("dwh-mcp",))
    with pytest.raises(AuthError) as err:
        p.gate(RequestIdentity(shared=False, subject="alice", claims={"aud": "plane-mcp"}))
    assert err.value.context == {"reason": "wrong_audience", "expected_audience": ["dwh-mcp"]}


def test_community_edition_refusal_is_tagged():
    from beherouter.plugins.plane import community_edition_guard

    with pytest.raises(UsageError) as err:
        community_edition_guard("workitem", {"action": "list"})
    assert err.value.context["reason"] == "edition_unsupported"


async def test_exchange_failures_are_tagged(monkeypatch):
    from beherouter import tokenexchange

    class Boom:
        async def token(self, subject_token):
            raise Unavailable("the token endpoint answered 503")

    class Refused:
        async def token(self, subject_token):
            raise AuthError("the identity provider refused")

    for exchanger, reason in ((Boom(), "identity_unavailable"), (Refused(), "unauthenticated")):
        monkeypatch.setattr(tokenexchange, "exchanger_for", lambda s, c, e=exchanger: e)
        with pytest.raises((Unavailable, AuthError)) as err:
            await tokenexchange.exchanged_headers("s", None, "tok", "authorization", "Bearer ")
        assert err.value.context["reason"] == reason

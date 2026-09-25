"""`health --deep --bearer-file`: probe AS a user, not as the deployment.

With an identity mode, a green probe proves the deployment credential and
nothing else (`probe_scope: deployment-credential`). A production deployment
had to build its own gate for the missing half — an in-cluster pod calling
`member me` with a real user token and asserting the email that came back.
This is that gate, through the gateway's own verifier, gate and
materialisation, so it fails exactly where a real call would.
"""

import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from beherouter.auth import GatewayJWTVerifier
from beherouter.errors import Unavailable
from beherouter.health import check_entry, failed
from beherouter.models import Backend, ToolDescriptor
from beherouter.registry import RegistryEntry

ISSUER = "https://idp.test"


class _Executor:
    """Answers `member me` as whoever the forwarded bearer names."""

    def __init__(self, answer=None, fail=False):
        self.calls = []
        self._answer = answer
        self._fail = fail

    async def run(self, verb, args, *, identity=None):
        self.calls.append((verb, args, identity))
        if self._fail and identity is not None:
            raise Unavailable("backend said 401")
        if identity is None:
            return {"result": {"id": "svc", "email": "svc@service.invalid"}}
        return {"result": self._answer}


def _loader(executor):
    async def load(entry):
        return Backend(
            name=entry.name,
            kind="mcp",
            descriptors=[
                ToolDescriptor(
                    name="member", verb="member", summary="", schema={},
                    pinned=True, mutating=False,
                )
            ],
            executor=executor,
        )

    return load


def _entry(identity=True, **authz):
    return RegistryEntry(
        name="plane",
        plugin="plane-http",
        config={"base_url": "http://plane-mcp-bearer:8211/bearer/mcp"},
        env={"access_token": "${BEHEROUTER_PLANE_ACCESS_TOKEN}"},
        pinned=["member"],
        identity={"mode": "bearer"} if identity else None,
        authz=authz or None,
    )


@pytest.fixture
def idp():
    kp = RSAKeyPair.generate()
    verifier = GatewayJWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience="gw")

    def mint(**kw):
        kw.setdefault("audience", "gw")
        return kp.create_token(subject="alice-id", issuer=ISSUER, **kw)

    return mint, verifier


async def test_the_users_identity_reaches_the_backend_and_matches(idp):
    mint, verifier = idp
    token = mint(additional_claims={"email": "alice@bank.invalid"})
    ex = _Executor(answer={"id": "p-1", "email": "Alice@bank.invalid"})
    rec = await check_entry(_entry(), load=_loader(ex), user_token=token, verifier=verifier)
    up = rec["user_probe"]
    assert up["state"] == "ok"
    assert up["matches_caller"] is True
    assert up["backend_identity"]["email"] == "Alice@bank.invalid"
    # the per-user call carried the caller's own bearer
    ident = ex.calls[-1][2]
    assert ident.headers["authorization"] == f"Bearer {token}"
    assert rec["probe"] == "ok"  # the deployment probe still ran, separately
    assert failed([rec]) == []


async def test_a_backend_answering_as_someone_else_is_a_failure(idp):
    """The shape of the incident this exists for: attached green, and every
    user's call quietly acting as the deployment identity."""
    mint, verifier = idp
    token = mint(additional_claims={"email": "alice@bank.invalid"})
    ex = _Executor(answer={"id": "svc", "email": "svc@service.invalid"})
    rec = await check_entry(_entry(), load=_loader(ex), user_token=token, verifier=verifier)
    assert rec["user_probe"]["state"] == "mismatch"
    assert failed([rec]) == ["plane"]


async def test_an_expired_token_is_rejected_saying_expired(idp):
    mint, verifier = idp
    token = mint(expires_in_seconds=-60)
    rec = await check_entry(
        _entry(), load=_loader(_Executor()), user_token=token, verifier=verifier
    )
    assert rec["user_probe"] == {"state": "rejected", "reason": "expired"}
    assert failed([rec]) == ["plane"]


async def test_a_token_the_surface_refuses_is_refused(idp):
    mint, verifier = idp
    token = mint()  # aud "gw" only
    rec = await check_entry(
        _entry(audience="plane-mcp"),
        load=_loader(_Executor()),
        user_token=token,
        verifier=verifier,
    )
    assert rec["user_probe"]["state"] == "refused"
    assert "plane-mcp" in rec["user_probe"]["error"]


async def test_a_failing_per_user_call_is_failed_while_the_deployment_probe_is_green(idp):
    mint, verifier = idp
    ex = _Executor(fail=True)
    rec = await check_entry(_entry(), load=_loader(ex), user_token=mint(), verifier=verifier)
    assert rec["probe"] == "ok"
    assert rec["user_probe"]["state"] == "failed"
    assert failed([rec]) == ["plane"]


async def test_a_surface_without_an_identity_mode_is_not_applicable(idp):
    mint, verifier = idp
    rec = await check_entry(
        _entry(identity=False),
        load=_loader(_Executor()),
        user_token=mint(),
        verifier=verifier,
    )
    assert rec["user_probe"]["state"] == "not_applicable"
    assert failed([rec]) == []


async def test_an_answer_with_no_identity_fields_is_unknown_not_a_failure(idp):
    mint, verifier = idp
    ex = _Executor(answer="pong")
    rec = await check_entry(_entry(), load=_loader(ex), user_token=mint(), verifier=verifier)
    assert rec["user_probe"]["state"] == "ok"
    assert rec["user_probe"]["matches_caller"] is None


def test_the_cli_reads_the_token_from_a_file_never_argv(tmp_path):
    from beherouter.cli.app import _read_bearer

    path = tmp_path / "token"
    path.write_text("Bearer abc.def.ghi\n")
    assert _read_bearer(str(path)) == "abc.def.ghi"


def test_the_cli_refuses_bearer_file_without_deep(tmp_path, monkeypatch, capsys):
    from beherouter.cli.app import app

    monkeypatch.setenv("BEHEROUTER_REGISTRY", str(tmp_path / "registry.toml"))
    path = tmp_path / "token"
    path.write_text("abc")
    assert app.main(["health", "--bearer-file", str(path), "--json"]) != 0

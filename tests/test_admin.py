"""Admin API (spec §4)."""

import json
import logging
import os

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from reload_harness import running, write

from beherouter import auth, metrics
from beherouter.admin import AdminAuth, check_admin_config, subject_digest
from beherouter.errors import UsageError
from beherouter.killswitch import _SWITCHES

ADMIN = {"Authorization": "Bearer adm1n"}
ISSUER = "https://idp.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BEHEROUTER_GATEWAY_TOKEN", "s3cret")
    monkeypatch.setenv("BEHEROUTER_RELOAD_DRAIN_S", "0")
    monkeypatch.setenv("BEHEROUTER_ADMIN_TOKEN", "adm1n")
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(tmp_path / "ks.json"))
    # a test that ran logsetup.configure() may have detached the audit logger
    monkeypatch.setattr(logging.getLogger("beherouter.audit"), "propagate", True)
    _SWITCHES.clear()
    yield
    _SWITCHES.clear()


@pytest.fixture
def oidc(monkeypatch):
    """A `both` gateway whose JWT verifier trusts a local key pair (no JWKS
    fetch), with `gw-admin` as the admin role. Returns `jwt(sub, roles)`."""
    from beherouter.auth import GatewayJWTVerifier

    kp = RSAKeyPair.generate()
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.setenv("BEHEROUTER_OIDC_ROLES_CLAIM", "roles")
    monkeypatch.setenv("BEHEROUTER_ADMIN_ROLE", "gw-admin")
    monkeypatch.setattr(
        auth,
        "oidc_verifier",
        lambda: GatewayJWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience="gw"),
    )

    def jwt(sub: str, roles: list[str]) -> str:
        return kp.create_token(
            subject=sub, issuer=ISSUER, audience="gw", additional_claims={"roles": roles}
        )

    return jwt


@pytest.fixture
def rejections():
    metrics.AUTH_REJECTIONS.clear()
    auth.seed_rejections()

    def read() -> dict[tuple[str, str], float]:
        out = {}
        for family in metrics.REGISTRY.collect():
            if family.name == "beherouter_auth_rejections":
                for s in family.samples:
                    if s.name.endswith("_total") and s.value:
                        out[(s.labels["reason"], s.labels["surface"])] = s.value
        return out

    yield read
    metrics.AUTH_REJECTIONS.clear()
    auth.seed_rejections()


def _audit(caplog) -> list[dict]:
    return [
        json.loads(rec.getMessage())
        for rec in caplog.records
        if rec.name == "beherouter.audit"
    ]


def test_from_env_is_none_without_either_variable(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_ADMIN_TOKEN")
    monkeypatch.delenv("BEHEROUTER_ADMIN_ROLE", raising=False)
    assert AdminAuth.from_env() is None


def test_from_env_reads_both_variables(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_ADMIN_ROLE", " gw-admin ")
    assert AdminAuth.from_env() == AdminAuth(token="adm1n", role="gw-admin")


def test_admin_token_equal_to_the_gateway_token_refuses_boot():
    with pytest.raises(UsageError, match="must differ"):
        check_admin_config(AdminAuth(token="s3cret", role=None))


def test_a_distinct_admin_token_and_no_config_pass():
    check_admin_config(None)
    check_admin_config(AdminAuth(token="adm1n", role=None))


def test_admin_role_needs_oidc_and_a_roles_claim(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "shared")
    with pytest.raises(UsageError, match="BEHEROUTER_AUTH_MODE"):
        check_admin_config(AdminAuth(token=None, role="gw-admin"))
    monkeypatch.setenv("BEHEROUTER_AUTH_MODE", "both")
    monkeypatch.delenv("BEHEROUTER_OIDC_ROLES_CLAIM", raising=False)
    with pytest.raises(UsageError, match="BEHEROUTER_OIDC_ROLES_CLAIM"):
        check_admin_config(AdminAuth(token=None, role="gw-admin"))


async def test_boot_refuses_an_admin_token_equal_to_the_gateway_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_ADMIN_TOKEN", "s3cret")
    reg = tmp_path / "r.toml"
    write(reg, "")
    with pytest.raises(UsageError, match="must differ"):
        async with running(reg):
            pass


def test_subject_digest_is_short_and_stable():
    assert subject_digest("u") == subject_digest("u")
    assert subject_digest("u").startswith("sha256:") and len(subject_digest("u")) == 15
    assert subject_digest("u") != subject_digest("v")


async def test_no_admin_routes_without_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("BEHEROUTER_ADMIN_TOKEN")
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 404
        assert (await c.get("/admin/killswitch", headers=ADMIN)).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "Bearer s3cret"},
                                     {"Authorization": "Basic adm1n"}])
async def test_bad_credentials_are_401(tmp_path, headers):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        r = await c.post("/admin/reload", headers=headers)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"


async def test_reload_through_the_api(tmp_path, gateway_plugin, exec_builder, caplog):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n')
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            r = await c.post("/admin/reload", headers=ADMIN)
        assert r.status_code == 200 and r.json()["added"] == ["a"]
        assert r.json()["trigger"] == "admin"
        write(reg, '[a]\nplugin = "nope"\n')
        r = await c.post("/admin/reload", headers=ADMIN)
        assert r.status_code == 422 and "unknown plugin" in r.json()["detail"]
        assert r.headers["content-type"].startswith("application/problem+json")
    [line] = [ln for ln in _audit(caplog) if ln["event"] == "admin"]
    assert {k: line[k] for k in ("action", "target_kind", "target", "actor", "outcome")} == {
        "action": "reload", "target_kind": "registry", "target": "-",
        "actor": "<admin-token>", "outcome": "ok",
    }


async def test_a_reload_that_raises_is_503_and_logged(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, c):
        async def boom(trigger):
            raise ValueError("a bug in the reload")

        rt.reloader._apply = boom  # Reloader hands the apply's exception to the requester
        with caplog.at_level(logging.ERROR, logger="beherouter.admin"):
            r = await c.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    [rec] = [rec for rec in caplog.records if rec.name == "beherouter.admin"]
    assert rec.levelno == logging.ERROR and rec.exc_info is not None
    assert rec.exc_info[0] is ValueError


async def test_a_reload_after_shutdown_began_is_503_not_422(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, c):
        await rt.reloader.cancel()
        assert rt.reloader.closed
        with caplog.at_level(logging.ERROR, logger="beherouter.admin"):
            r = await c.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 503
    assert "shutting down" in r.json()["detail"]
    assert not [rec for rec in caplog.records if rec.name == "beherouter.admin"]


async def test_a_cancelled_reload_at_shutdown_is_503_without_a_traceback(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (rt, c):
        async def cancelled(trigger):
            rt.reloader._closed = True  # shutdown began while the reload ran
            raise RuntimeError("reload cancelled")

        rt.reloader.request = cancelled
        with caplog.at_level(logging.ERROR, logger="beherouter.admin"):
            r = await c.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 503
    assert not [rec for rec in caplog.records if rec.name == "beherouter.admin"]


async def test_admin_rejection_reasons_are_seeded_only_when_configured(
    tmp_path, monkeypatch
):
    reg = tmp_path / "r.toml"
    write(reg, "")
    series = ('beherouter_auth_rejections_total{reason="missing",surface=""} 0.0',
              'beherouter_auth_rejections_total{reason="missing_role",surface=""} 0.0')
    metrics.AUTH_REJECTIONS.clear()
    try:
        async with running(reg) as (_rt, c):
            body = (await c.get("/metrics")).text
        assert all(s in body for s in series)
        metrics.AUTH_REJECTIONS.clear()
        monkeypatch.delenv("BEHEROUTER_ADMIN_TOKEN")
        async with running(reg) as (_rt, c):
            body = (await c.get("/metrics")).text
        assert not any(s in body for s in series)
    finally:
        metrics.AUTH_REJECTIONS.clear()
        auth.seed_rejections()


async def test_kill_switch_round_trip_and_audit(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            r = await c.post("/admin/killswitch/subjects/block", headers=ADMIN,
                             json={"sub": "user-123", "reason": "offboarded"})
            assert r.status_code == 200 and "user-123" in r.json()["state"]["subjects"]
            r = await c.put("/admin/killswitch/surfaces/dwh", headers=ADMIN, json={})
            assert r.json()["warning"] == "no such surface"
            assert (await c.get("/healthz")).json()["disabled"] == ["dwh"]
            r = await c.put("/admin/killswitch/all", headers=ADMIN, json={"reason": "INC"})
            assert r.json()["state"]["all"]["reason"] == "INC"
            assert (await c.get("/healthz")).json()["disabled"] == ["*"]
            await c.delete("/admin/killswitch/all", headers=ADMIN)
            await c.delete("/admin/killswitch/surfaces/dwh", headers=ADMIN)
            r = await c.post("/admin/killswitch/subjects/unblock", headers=ADMIN,
                             json={"sub": "user-123"})
            assert r.json()["state"]["subjects"] == {}
            assert (await c.get("/admin/killswitch", headers=ADMIN)).json()["subjects"] == {}
    admin_lines = [ln for ln in _audit(caplog) if ln["event"] == "admin"]
    assert [ln["action"] for ln in admin_lines] == [
        "block_subject", "disable_surface", "stop_all", "resume_all", "enable_surface",
        "unblock_subject", "read_killswitch",
    ]
    assert {ln["outcome"] for ln in admin_lines} == {"ok"}
    targets = {ln["action"]: ln["target"] for ln in admin_lines}
    assert targets["stop_all"] == "*" and targets["disable_surface"] == "dwh"
    assert targets["read_killswitch"] == "-"
    blob = json.dumps(admin_lines) + caplog.text
    assert "user-123" not in blob and "offboarded" not in blob
    assert targets["block_subject"] == targets["unblock_subject"] == subject_digest("user-123")


async def test_an_existing_surface_has_no_warning(tmp_path, gateway_plugin, exec_builder):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, '[a]\nplugin = "t-ok"\n')
    async with running(reg) as (_rt, c):
        r = await c.put("/admin/killswitch/surfaces/a", headers=ADMIN, json={"reason": "q"})
    assert r.status_code == 200 and "warning" not in r.json()
    assert r.json()["state"]["surfaces"]["a"]["by"] == "<admin-token>"


async def test_a_wrong_method_is_405(tmp_path):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        assert (await c.get("/admin/killswitch/all", headers=ADMIN)).status_code == 405
        assert (await c.get("/admin/reload", headers=ADMIN)).status_code == 405


async def test_refusals_are_audited(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            await c.get("/admin/killswitch")
            await c.post("/admin/killswitch/subjects/block", json={"sub": "user-123"})
            await c.post("/admin/reload", headers={"Authorization": "Bearer wrong"})
    lines = [ln for ln in _audit(caplog) if ln["event"] == "admin"]
    assert [(ln["action"], ln["actor"], ln["outcome"], ln["target"]) for ln in lines] == [
        ("read_killswitch", "<refused>", "refused", "-"),
        ("block_subject", "<refused>", "refused", "-"),
        ("reload", "<refused>", "refused", "-"),
    ]


async def test_audit_off_silences_admin_lines(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("BEHEROUTER_AUDIT", "off")
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            assert (await c.put("/admin/killswitch/all", headers=ADMIN)).status_code == 200
    assert _audit(caplog) == []


async def test_sub_in_body_is_validated(tmp_path, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            r = await c.post("/admin/killswitch/subjects/block", headers=ADMIN, json={"sub": ""})
            assert r.status_code == 400
            r = await c.post("/admin/killswitch/subjects/block", headers=ADMIN,
                             json={"sub": "x" * 513})
            assert r.status_code == 400
            r = await c.put("/admin/killswitch/all", headers=ADMIN, json={"reason": "x" * 201})
            assert r.status_code == 400
            r = await c.put("/admin/killswitch/all", headers=ADMIN, json={"reason": 5})
            assert r.status_code == 400
            r = await c.post("/admin/killswitch/subjects/block", headers=ADMIN, content=b"{nope")
            assert r.status_code == 400
            r = await c.post("/admin/killswitch/subjects/block", headers=ADMIN, json=["a"])
            assert r.status_code == 400
            assert r.headers["content-type"].startswith("application/problem+json")
        assert (await c.get("/admin/killswitch", headers=ADMIN)).json() == {
            "surfaces": {}, "subjects": {}
        }
    assert {ln["outcome"] for ln in _audit(caplog)} == {"bad_request"}


async def test_read_only_state_file_is_409(tmp_path, monkeypatch, caplog):
    if os.geteuid() == 0:
        pytest.skip("root writes to a read-only directory")
    d = tmp_path / "ro"
    d.mkdir()
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(d / "ks.json"))
    _SWITCHES.clear()
    d.chmod(0o500)
    try:
        reg = tmp_path / "r.toml"
        write(reg, "")
        async with running(reg) as (_rt, c):
            with caplog.at_level(logging.INFO, logger="beherouter.audit"):
                r = await c.put("/admin/killswitch/all", headers=ADMIN, json={})
        assert r.status_code == 409
        assert "read-only" in r.json()["detail"]
        assert [ln["outcome"] for ln in _audit(caplog)] == ["conflict"]
    finally:
        d.chmod(0o700)


async def test_killswitch_routes_absent_without_the_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BEHEROUTER_KILLSWITCH_PATH")
    _SWITCHES.clear()
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        assert (await c.get("/admin/killswitch", headers=ADMIN)).status_code == 404
        assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 200


async def test_admin_routes_stay_ahead_of_the_surfaces_across_a_reload(
    tmp_path, gateway_plugin, exec_builder
):
    gateway_plugin("t-ok", exec_builder([]))
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        write(reg, '[a]\nplugin = "t-ok"\n')
        assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 200
        assert (await c.get("/admin/killswitch", headers=ADMIN)).status_code == 200


# --- the OIDC role ------------------------------------------------------------


async def test_the_admin_role_passes_and_its_absence_is_403(tmp_path, oidc, caplog):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            ok = await c.put(
                "/admin/killswitch/all",
                headers={"Authorization": f"Bearer {oidc('ops-alice', ['gw-admin'])}"},
            )
            no = await c.put(
                "/admin/killswitch/all",
                headers={"Authorization": f"Bearer {oidc('bob', ['reader'])}"},
            )
            token = await c.put("/admin/killswitch/all", headers=ADMIN)
    assert ok.status_code == 200
    assert ok.json()["state"]["all"]["by"] == "ops-alice"
    assert no.status_code == 403
    assert no.headers["content-type"].startswith("application/problem+json")
    assert token.status_code == 200  # the admin token still passes beside the role
    lines = [ln for ln in _audit(caplog) if ln["event"] == "admin"]
    assert [(ln["actor"], ln["outcome"]) for ln in lines] == [
        ("ops-alice", "ok"), ("<refused>", "refused"), ("<admin-token>", "ok"),
    ]


async def test_the_role_alone_enables_the_api(tmp_path, oidc, monkeypatch):
    monkeypatch.delenv("BEHEROUTER_ADMIN_TOKEN")
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 401
        r = await c.post(
            "/admin/reload",
            headers={"Authorization": f"Bearer {oidc('ops-alice', ['gw-admin'])}"},
        )
    assert r.status_code == 200


async def test_each_refusal_counts_once_with_no_surface(tmp_path, oidc, rejections):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        assert (await c.post("/admin/reload")).status_code == 401
        assert rejections() == {("missing", ""): 1}
        bad = {"Authorization": "Bearer not-a-jwt"}
        assert (await c.post("/admin/reload", headers=bad)).status_code == 401
        assert rejections() == {("missing", ""): 1, ("invalid", ""): 1}
        shared = {"Authorization": "Bearer s3cret"}
        assert (await c.post("/admin/reload", headers=shared)).status_code == 401
        assert rejections() == {("missing", ""): 1, ("invalid", ""): 2}
        role_less = {"Authorization": f"Bearer {oidc('bob', ['reader'])}"}
        assert (await c.post("/admin/reload", headers=role_less)).status_code == 403
        assert rejections() == {
            ("missing", ""): 1, ("invalid", ""): 2, ("missing_role", ""): 1
        }
        admin = {"Authorization": f"Bearer {oidc('ops', ['gw-admin'])}"}
        assert (await c.post("/admin/reload", headers=admin)).status_code == 200
        assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 200
        assert rejections() == {
            ("missing", ""): 1, ("invalid", ""): 2, ("missing_role", ""): 1
        }


async def test_a_token_only_gateway_counts_a_wrong_token_once(tmp_path, rejections):
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        r = await c.post("/admin/reload", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert rejections() == {("invalid", ""): 1}


# --- never echo (spec §8) -----------------------------------------------------

FILE_SECRET = "FILE-SECRET-5d0c9e1b"
BLOCKED = "blocked-sub-7a4f2e66"


class _Every(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def every_record():
    """Every record of every logger at DEBUG, including the ones that do not
    propagate to the root (where caplog alone would miss them)."""
    handler = _Every()
    root = logging.getLogger()
    saved = []
    loggers = [root] + [
        lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        saved.append((lg, lg.level))
        lg.setLevel(logging.DEBUG)
        if lg is root or not lg.propagate:
            lg.addHandler(handler)
    yield handler.records
    for lg, level in saved:
        lg.setLevel(level)
        lg.removeHandler(handler)


async def test_no_blocked_sub_and_no_file_secret_is_ever_echoed(
    tmp_path, oidc, every_record, monkeypatch
):
    from beherouter.models import Backend
    from beherouter.plugins import PLUGINS, register
    from beherouter.plugins.spec import EnvVar, PluginSpec

    class _E:
        async def run(self, verb, args, *, identity=None):
            return {}

        async def aclose(self):
            pass

    async def build(ctx):
        assert ctx.env["api_key"] == FILE_SECRET
        return Backend(name=ctx.surface, kind="mcp", descriptors=[], executor=_E())

    register(
        PluginSpec(name="t-secret", summary="t", backing="inproc", env=(EnvVar("api_key"),)),
        build,
    )
    secret = tmp_path / "key"
    secret.write_text(FILE_SECRET + "\n")
    ks = tmp_path / "ks.json"
    ks.write_text(json.dumps({"subjects": {BLOCKED: {"reason": "offboarded"}}}))
    reg = tmp_path / "r.toml"
    write(reg, f'[a]\nplugin = "t-secret"\n[a.env]\napi_key = "${{file:{secret}}}"\n')
    try:
        async with running(reg) as (_rt, c):
            asgi = httpx.ASGITransport(app=c.app)
            mcp = Client(
                StreamableHttpTransport(
                    "http://t/a/mcp",
                    auth=oidc(BLOCKED, []),
                    httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=asgi, **kw),
                )
            )
            async with mcp:
                refused = await mcp.call_tool_mcp("search_tools", {"query": "q"})
            assert refused.isError
            assert refused.meta["io.beherouter/error"]["reason"] == "caller_blocked"
            assert BLOCKED not in refused.content[0].text

            for path, body in (
                ("unblock", {"sub": BLOCKED}),
                ("block", {"sub": BLOCKED, "reason": "REASON-9f1c"}),
            ):
                r = await c.post(f"/admin/killswitch/subjects/{path}", headers=ADMIN, json=body)
                assert r.status_code == 200
            assert (await c.post("/admin/reload", headers=ADMIN)).status_code == 200
            healthz = (await c.get("/healthz")).text
            metrics_body = (await c.get("/metrics")).text
    finally:
        PLUGINS.pop("t-secret", None)

    def is_own_call_line(rec: logging.LogRecord) -> bool:
        """The blocked caller's OWN per-call audit line, which carries its sub
        as every call's line does (the sanctioned exception). Nothing else."""
        if rec.name != "beherouter.audit":
            return False
        line = json.loads(rec.getMessage())
        return (
            line.get("event") == "tool_call"
            and line.get("caller", {}).get("sub") == BLOCKED
            and line.get("reason") == "caller_blocked"
        )

    own = [rec for rec in every_record if is_own_call_line(rec)]
    assert len(own) == 1
    rest = [rec for rec in every_record if not is_own_call_line(rec)]
    admin_lines = [
        json.loads(rec.getMessage())
        for rec in rest
        if rec.name == "beherouter.audit" and '"event":"admin"' in rec.getMessage()
    ]
    assert {ln["action"] for ln in admin_lines} == {"unblock_subject", "block_subject", "reload"}
    for rec in rest:
        text = rec.getMessage() + repr(rec.args) + str(rec.__dict__.get("exc_text") or "")
        assert BLOCKED not in text, rec.name
        assert FILE_SECRET not in text, rec.name
        assert "offboarded" not in text and "REASON-9f1c" not in text, rec.name
    for body in (healthz, metrics_body):
        assert BLOCKED not in body and FILE_SECRET not in body


async def test_a_change_the_state_file_refuses_is_400_and_audited(
    tmp_path, monkeypatch, caplog
):
    """No route builds an invalid change today; if one ever does, the API answers
    400 rather than writing it or failing as a 500."""
    from beherouter.killswitch import KillSwitch

    async def refuse(self, change, *, actor):
        raise UsageError("that change would leave an invalid kill-switch file (x)")

    monkeypatch.setattr(KillSwitch, "update", refuse)
    reg = tmp_path / "r.toml"
    write(reg, "")
    async with running(reg) as (_rt, c):
        with caplog.at_level(logging.INFO, logger="beherouter.audit"):
            r = await c.put("/admin/killswitch/surfaces/dwh", headers=ADMIN, json={})
    assert r.status_code == 400 and "invalid kill-switch file" in r.json()["detail"]
    assert [(ln["action"], ln["outcome"]) for ln in _audit(caplog)] == [
        ("disable_surface", "bad_request")
    ]

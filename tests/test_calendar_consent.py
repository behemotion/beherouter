"""Per-user calendar consent: the flow, the map writer, and the refusals.

No real network and no real browser: the token endpoint is an
httpx.MockTransport, and the browser is a function that reads the authorize URL
and answers the receiver the way a provider's redirect would.
"""

import asyncio
import os
import stat
import threading
import tomllib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from beherouter.errors import AuthError, NotFound, Unavailable, UsageError
from beherouter.identity import SecretMap
from beherouter.plugins.calendar import consent
from beherouter.plugins.calendar.consent import (
    GOOGLE,
    MICROSOFT,
    LoopbackReceiver,
    authorize_url,
    pkce_pair,
    remove_names,
    resolve_target,
    upsert_subject,
)
from beherouter.plugins.calendar.providers import microsoft
from beherouter.registry import RegistryEntry

REFRESH = "1//rt-SECRET-never-printed"


def _entry(plugin="gcal", tmp_path=None, **identity):
    env = {"client_id": "${BEHE_T_CID}", "refresh_token": "${BEHE_T_RT}"}
    if plugin == "gcal":
        env["client_secret"] = "${BEHE_T_SEC}"
    ident = {
        "mode": "lookup",
        "key": "email",
        "map": {"refresh_token": f"{plugin}_refresh_token"},
    }
    if tmp_path is not None:
        ident["path"] = str(tmp_path / "identity-map.toml")
    ident.update(identity)
    return RegistryEntry(name=plugin, plugin=plugin, env=env, identity=ident)


@pytest.fixture(autouse=True)
def client_env(monkeypatch):
    monkeypatch.setenv("BEHE_T_CID", "cid-123")
    monkeypatch.setenv("BEHE_T_SEC", "client-SECRET")
    monkeypatch.setenv("BEHE_T_RT", "deployment-rt")


class FakeReceiver:
    """Plays the provider's redirect: answers with the state it was sent."""

    redirect_uri = "http://localhost:8765"

    def __init__(self, **override):
        self.url = None
        self.override = override
        self.closed = False

    def browse(self, url):
        self.url = url

    def wait(self, timeout):
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.url).query).items()}
        return {"code": "auth-code", "state": q["state"], **self.override}

    def close(self):
        self.closed = True


def _token_endpoint(seen, *, status=200, body=None):
    def handler(request):
        seen.append((str(request.url), dict(parse_qs(request.content.decode()))))
        payload = (
            body
            if body is not None
            else {
                "access_token": "at",
                "refresh_token": REFRESH,
                "expires_in": 3599,
            }
        )
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _grant(entry, receiver, http, **kw):
    return await consent.grant(
        entry,
        subject=kw.pop("subject", "alice@example.test"),
        receiver=receiver,
        open_url=receiver.browse,
        http=http,
        **kw,
    )


# --- the provider traps -------------------------------------------------------


def test_microsoft_consent_uses_the_consumers_authority():
    """⚠️ The authorize URL is derived from the held TOKEN_URL constant, so the
    consent flow cannot drift back to `common` on its own."""
    assert MICROSOFT.token_url == microsoft.TOKEN_URL
    assert "/consumers/" in MICROSOFT.authorize_url
    assert "/common/" not in MICROSOFT.authorize_url
    assert MICROSOFT.authorize_url.endswith("/oauth2/v2.0/authorize")


def test_google_authorize_url_asks_for_a_refresh_token():
    """Without access_type=offline AND prompt=consent Google silently issues no
    refresh token."""
    url = authorize_url(
        GOOGLE, client_id="c", redirect_uri="http://localhost:1", state="s", challenge="x"
    )
    q = parse_qs(urlsplit(url).query)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar"]
    assert q["code_challenge_method"] == ["S256"]


def test_microsoft_authorize_url_requests_offline_access():
    url = authorize_url(
        MICROSOFT, client_id="c", redirect_uri="http://localhost:1", state="s", challenge="x"
    )
    assert "offline_access" in parse_qs(urlsplit(url).query)["scope"][0]


def test_pkce_challenge_is_s256_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()


# --- the grant ----------------------------------------------------------------


async def test_a_google_grant_lands_in_the_map_under_the_mapped_name(tmp_path):
    seen: list = []
    receiver = FakeReceiver()
    out = await _grant(_entry("gcal", tmp_path), receiver, _token_endpoint(seen))

    path = tmp_path / "identity-map.toml"
    data = tomllib.loads(path.read_text())
    assert data == {"alice@example.test": {"gcal_refresh_token": REFRESH}}
    assert out["written"] == ["gcal_refresh_token"]
    assert out["key_claim"] == "email"
    assert any("Production" in w for w in out["warnings"])

    url, form = seen[0]
    assert url == GOOGLE.token_url
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == ["cid-123"]
    assert form["client_secret"] == ["client-SECRET"]
    assert form["redirect_uri"] == [receiver.redirect_uri]
    assert form["code_verifier"]
    # login_hint steers the browser to the subject's account
    assert parse_qs(urlsplit(receiver.url).query)["login_hint"] == ["alice@example.test"]
    assert receiver.closed


async def test_the_written_grant_is_what_lookup_reads(tmp_path):
    """End to end against the gateway's own reader, not a re-implementation."""
    await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]))
    found = SecretMap(tmp_path / "identity-map.toml").credentials_for(
        "gcal", "alice@example.test", "email"
    )
    assert found == {"gcal_refresh_token": REFRESH}


async def test_a_microsoft_grant_is_a_public_client_exchange(tmp_path):
    seen: list = []
    await _grant(_entry("m365", tmp_path), FakeReceiver(), _token_endpoint(seen))
    url, form = seen[0]
    assert url == microsoft.TOKEN_URL
    assert "client_secret" not in form
    assert form["scope"] == [microsoft.SCOPE]


async def test_the_map_file_is_0600_and_the_token_is_never_emitted(tmp_path, capsys):
    out = await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]))
    mode = stat.S_IMODE(os.stat(tmp_path / "identity-map.toml").st_mode)
    assert mode == 0o600
    captured = capsys.readouterr()
    for text in (repr(out), captured.out, captured.err):
        assert REFRESH not in text
        assert "client-SECRET" not in text


async def test_a_grant_keeps_other_subjects_other_names_and_comments(tmp_path):
    path = tmp_path / "identity-map.toml"
    path.write_text(
        '# rendered by hand\n["bob@example.test"]\nm365_refresh_token = "bob-rt"\n\n'
        '["alice@example.test"]\nm365_refresh_token = "alice-ms"\n'
    )
    os.chmod(path, 0o644)
    await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]))
    text = path.read_text()
    assert "# rendered by hand" in text
    data = tomllib.loads(text)
    assert data["bob@example.test"] == {"m365_refresh_token": "bob-rt"}
    assert data["alice@example.test"] == {
        "m365_refresh_token": "alice-ms",
        "gcal_refresh_token": REFRESH,
    }
    # rewritten 0600 even though it was 0644 before
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


async def test_a_regrant_replaces_the_subjects_token(tmp_path):
    path = tmp_path / "identity-map.toml"
    upsert_subject(path, "alice@example.test", {"gcal_refresh_token": "old"})
    await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]))
    assert tomllib.loads(path.read_text())["alice@example.test"] == {"gcal_refresh_token": REFRESH}


async def test_a_per_user_client_is_written_beside_the_token(tmp_path):
    entry = _entry(
        "gcal",
        tmp_path,
        map={
            "refresh_token": "gcal_rt",
            "client_id": "gcal_cid",
            "client_secret": "gcal_secret",
        },
    )
    await _grant(entry, FakeReceiver(), _token_endpoint([]))
    data = tomllib.loads((tmp_path / "identity-map.toml").read_text())
    assert data["alice@example.test"] == {
        "gcal_rt": REFRESH,
        "gcal_cid": "cid-123",
        "gcal_secret": "client-SECRET",
    }


async def test_client_flags_override_the_entrys_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BEHE_T_CID")
    monkeypatch.delenv("BEHE_T_SEC")
    secret = tmp_path / "secret"
    secret.write_text("flag-secret\n")
    seen: list = []
    await _grant(
        _entry("gcal", tmp_path),
        FakeReceiver(),
        _token_endpoint(seen),
        client_id="flag-cid",
        client_secret_file=str(secret),
    )
    form = seen[0][1]
    assert (form["client_id"], form["client_secret"]) == (["flag-cid"], ["flag-secret"])


async def test_an_unset_client_var_names_the_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("BEHE_T_CID")
    with pytest.raises(UsageError, match="--client-id"):
        await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]))


async def test_a_public_client_refuses_a_client_secret(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x")
    with pytest.raises(UsageError, match="public client"):
        await _grant(
            _entry("m365", tmp_path),
            FakeReceiver(),
            _token_endpoint([]),
            client_secret_file=str(secret),
        )


# --- failures write nothing ---------------------------------------------------


async def test_no_refresh_token_in_the_response_is_refused(tmp_path):
    http = _token_endpoint([], body={"access_token": "at", "expires_in": 3599})
    with pytest.raises(AuthError, match="no refresh token"):
        await _grant(_entry("gcal", tmp_path), FakeReceiver(), http)
    assert not (tmp_path / "identity-map.toml").exists()


async def test_a_rejected_exchange_names_the_code_never_the_form(tmp_path):
    http = _token_endpoint([], status=400, body={"error": "invalid_grant"})
    with pytest.raises(AuthError, match="invalid_grant") as e:
        await _grant(_entry("gcal", tmp_path), FakeReceiver(), http)
    assert "auth-code" not in str(e.value)
    assert "client-SECRET" not in str(e.value)
    assert not (tmp_path / "identity-map.toml").exists()


async def test_a_wrong_state_is_refused_before_the_exchange(tmp_path):
    seen: list = []
    with pytest.raises(AuthError, match="state"):
        await _grant(_entry("gcal", tmp_path), FakeReceiver(state="forged"), _token_endpoint(seen))
    assert seen == []


async def test_a_denied_consent_is_refused(tmp_path):
    receiver = FakeReceiver(error="access_denied")
    with pytest.raises(AuthError, match="access_denied"):
        await _grant(_entry("gcal", tmp_path), receiver, _token_endpoint([]))


# --- what the surface must be -------------------------------------------------


def test_a_shared_credential_surface_is_refused():
    entry = RegistryEntry(name="gcal", plugin="gcal", env={})
    with pytest.raises(UsageError, match="not per-user"):
        resolve_target(entry)


def test_a_non_calendar_plugin_is_refused():
    entry = RegistryEntry(name="plane", plugin="plane", identity={"mode": "lookup"})
    with pytest.raises(UsageError, match="calendar-consent serves"):
        resolve_target(entry)


def test_a_map_that_does_not_forward_the_refresh_token_is_refused(tmp_path):
    with pytest.raises(UsageError, match="refresh_token"):
        resolve_target(_entry("gcal", tmp_path, map={"client_id": "cid"}))


def test_the_map_path_falls_back_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BEHEROUTER_IDENTITY_MAP", str(tmp_path / "env-map.toml"))
    assert resolve_target(_entry("gcal")).map_path == tmp_path / "env-map.toml"
    # --identity-map wins over both
    assert resolve_target(_entry("gcal", tmp_path), str(tmp_path / "x.toml")).map_path == (
        tmp_path / "x.toml"
    )


def test_no_map_path_anywhere_is_refused(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_IDENTITY_MAP", raising=False)
    with pytest.raises(UsageError, match="--identity-map"):
        resolve_target(_entry("gcal"))


async def test_a_symlinked_map_is_refused_before_the_browser(tmp_path):
    """A projected Kubernetes Secret is a symlink into a read-only directory."""
    real = tmp_path / "real.toml"
    real.write_text("")
    (tmp_path / "identity-map.toml").symlink_to(real)
    receiver = FakeReceiver()
    with pytest.raises(UsageError, match="symlink"):
        await _grant(_entry("gcal", tmp_path), receiver, _token_endpoint([]))
    assert receiver.url is None


async def test_an_empty_subject_is_refused(tmp_path):
    with pytest.raises(UsageError, match="--subject"):
        await _grant(_entry("gcal", tmp_path), FakeReceiver(), _token_endpoint([]), subject="")


# --- revoke -------------------------------------------------------------------


async def test_revoke_removes_the_token_and_revokes_it_at_google(tmp_path):
    path = tmp_path / "identity-map.toml"
    upsert_subject(
        path, "alice@example.test", {"gcal_refresh_token": "rt-a", "m365_refresh_token": "m"}
    )
    seen: list = []
    out = await consent.revoke(
        _entry("gcal", tmp_path), subject="alice@example.test", http=_token_endpoint(seen)
    )
    assert out["upstream"] == "revoked"
    assert out["removed"] == ["gcal_refresh_token"]
    assert seen == [(GOOGLE.revoke_url, {"token": ["rt-a"]})]
    assert tomllib.loads(path.read_text()) == {"alice@example.test": {"m365_refresh_token": "m"}}
    assert "rt-a" not in repr(out)


async def test_revoking_the_last_name_drops_the_subject(tmp_path):
    path = tmp_path / "identity-map.toml"
    upsert_subject(path, "alice@example.test", {"m365_refresh_token": "m"})
    upsert_subject(path, "bob@example.test", {"m365_refresh_token": "b"})
    out = await consent.revoke(_entry("m365", tmp_path), subject="alice@example.test")
    assert out["upstream"] == "not_supported"
    assert "account.live.com" in out["hint"]
    assert tomllib.loads(path.read_text()) == {"bob@example.test": {"m365_refresh_token": "b"}}


async def test_revoke_still_removes_locally_when_upstream_fails(tmp_path):
    path = tmp_path / "identity-map.toml"
    upsert_subject(path, "alice@example.test", {"gcal_refresh_token": "rt-a"})
    out = await consent.revoke(
        _entry("gcal", tmp_path),
        subject="alice@example.test",
        http=_token_endpoint([], status=503, body={}),
    )
    assert out["upstream"] == "failed: 503"
    assert tomllib.loads(path.read_text()) == {}


async def test_revoking_an_absent_subject_is_not_found(tmp_path):
    with pytest.raises(NotFound):
        await consent.revoke(_entry("gcal", tmp_path), subject="nobody@example.test")


def test_remove_names_on_an_absent_subject_is_not_found(tmp_path):
    path = tmp_path / "m.toml"
    upsert_subject(path, "a", {"x": "1"})
    with pytest.raises(NotFound):
        remove_names(path, "b", ["x"])


# --- the writer under concurrency ---------------------------------------------


def test_concurrent_upserts_lose_no_subject(tmp_path):
    """The lock serialises read-modify-write; without it, writers race."""
    path = tmp_path / "m.toml"
    threads = [
        threading.Thread(target=upsert_subject, args=(path, f"user{i}", {"rt": str(i)}))
        for i in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tomllib.loads(path.read_text())) == 16


def test_a_malformed_map_is_not_rewritten(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text("this is = = not toml")
    with pytest.raises(UsageError, match="not valid TOML"):
        upsert_subject(path, "a", {"x": "1"})
    assert path.read_text() == "this is = = not toml"


# --- the real loopback receiver ----------------------------------------------


async def test_the_loopback_receiver_takes_the_redirect():
    receiver = LoopbackReceiver(port=0)
    port = int(receiver.redirect_uri.rsplit(":", 1)[1])
    waiting = asyncio.create_task(asyncio.to_thread(receiver.wait, 10))

    def browser():
        with httpx.Client() as c:
            assert c.get(f"http://127.0.0.1:{port}/favicon.ico").status_code == 404
            r = c.get(f"http://127.0.0.1:{port}/?code=abc&state=xyz")
            assert r.status_code == 200
            assert "close this tab" in r.text

    await asyncio.to_thread(browser)
    assert await waiting == {"code": "abc", "state": "xyz"}


def test_the_loopback_receiver_times_out():
    receiver = LoopbackReceiver(port=0)
    with pytest.raises(Unavailable, match="no consent redirect"):
        receiver.wait(0.2)


def test_a_busy_port_is_a_usage_error():
    first = LoopbackReceiver(port=0)
    port = int(first.redirect_uri.rsplit(":", 1)[1])
    try:
        with pytest.raises(UsageError, match="--port"):
            LoopbackReceiver(port=port)
    finally:
        first.close()


def test_the_receiver_never_logs_the_code(capsys):
    receiver = LoopbackReceiver(port=0)
    port = int(receiver.redirect_uri.rsplit(":", 1)[1])
    t = threading.Thread(target=receiver.wait, args=(10,))
    t.start()
    httpx.get(f"http://127.0.0.1:{port}/?code=SECRET-CODE&state=s")
    t.join()
    assert "SECRET-CODE" not in capsys.readouterr().err


# --- the CLI verb -------------------------------------------------------------


def _cli(tmp_path, *args):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", "calendar-consent", *args, "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "BEHEROUTER_REGISTRY": str(tmp_path / "registry.toml")},
        check=False,
    )


def test_the_verb_refuses_a_surface_that_is_not_per_user(tmp_path):
    (tmp_path / "registry.toml").write_text('[gcal]\nplugin = "gcal"\n')
    r = _cli(tmp_path, "gcal", "--subject", "a@example.test")
    assert r.returncode == 2, r.stderr
    assert "not per-user" in r.stderr


def test_the_verb_revokes_through_the_registry(tmp_path):
    import json

    (tmp_path / "registry.toml").write_text(
        '[m365]\nplugin = "m365"\n  [m365.env]\n  client_id = "${X}"\n'
        '  refresh_token = "${Y}"\n  [m365.identity]\n  mode = "lookup"\n'
        f'  key = "email"\n  path = "{tmp_path / "map.toml"}"\n'
        '    [m365.identity.map]\n    refresh_token = "m365_rt"\n'
    )
    upsert_subject(tmp_path / "map.toml", "a@example.test", {"m365_rt": "rt"})
    r = _cli(tmp_path, "m365", "--subject", "a@example.test", "--revoke")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["removed"] == ["m365_rt"]
    assert tomllib.loads((tmp_path / "map.toml").read_text()) == {}


def test_the_verb_is_described_unpinned_and_mutating():
    import json
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "beherouter.cli.app", "describe", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    verbs = {v["name"]: v for v in json.loads(r.stdout)["verbs"]}
    assert verbs["calendar-consent"]["pinned"] is False
    assert verbs["calendar-consent"]["mutating"] is True

import pytest

from beherouter.errors import AuthError, Unavailable
from beherouter.identity import IdentityPolicy, RequestIdentity, SecretMap, secret_map

MAP = """
["alice@example.test"]
api_key = "pat-alice"

["bob@example.test"]
api_key = "pat-bob"
"""


def _map_file(tmp_path, text=MAP):
    path = tmp_path / "identity-map.toml"
    path.write_text(text)
    return path


def _user(email="alice@example.test") -> RequestIdentity:
    return RequestIdentity(
        shared=False, subject=email, claims={"sub": "u1", "email": email}
    )


def _policy(path) -> IdentityPolicy:
    return IdentityPolicy(
        surface="plane",
        mode="lookup",
        target="header",
        map={"x-api-key": "api_key"},
        key="email",
        path=str(path),
    )


def test_lookup_resolves_the_callers_own_credential(tmp_path):
    ident = _policy(_map_file(tmp_path)).materialise(_user())
    assert ident.headers == {"x-api-key": "pat-alice"}


def test_lookup_gives_each_caller_their_own(tmp_path):
    policy = _policy(_map_file(tmp_path))
    assert policy.materialise(_user("bob@example.test")).headers == {
        "x-api-key": "pat-bob"
    }


def test_a_caller_absent_from_the_map_is_refused(tmp_path):
    with pytest.raises(AuthError, match="identity map"):
        _policy(_map_file(tmp_path)).materialise(_user("carol@example.test"))


def test_a_credential_missing_from_the_entry_is_refused(tmp_path):
    path = _map_file(tmp_path, '["alice@example.test"]\nother = "x"\n')
    with pytest.raises(AuthError, match="api_key"):
        _policy(path).materialise(_user())


def test_a_missing_map_degrades_this_surface_only(tmp_path):
    with pytest.raises(Unavailable, match="unreadable"):
        _policy(tmp_path / "absent.toml").materialise(_user())


def test_an_unparsable_map_is_unavailable_not_a_crash(tmp_path):
    path = _map_file(tmp_path, "this is not toml {{{")
    with pytest.raises(Unavailable, match="could not be parsed"):
        _policy(path).materialise(_user())


def test_the_map_hot_reloads_when_the_file_changes(tmp_path):
    path = _map_file(tmp_path)
    sm = SecretMap(path)
    assert sm.credentials_for("plane", "alice@example.test", "email") == {
        "api_key": "pat-alice"
    }
    path.write_text('["alice@example.test"]\napi_key = "pat-rotated"\n')
    assert sm.credentials_for("plane", "alice@example.test", "email") == {
        "api_key": "pat-rotated"
    }


def test_status_reports_ok_missing_and_unparsable(tmp_path):
    assert SecretMap(_map_file(tmp_path)).status() == {"state": "ok", "entries": 2}
    assert SecretMap(tmp_path / "absent.toml").status()["state"] == "missing"
    bad = tmp_path / "bad.toml"
    bad.write_text("nope {{{")
    assert SecretMap(bad).status()["state"] == "unparsable"


def test_status_never_reports_a_credential(tmp_path):
    assert "pat-alice" not in repr(SecretMap(_map_file(tmp_path)).status())


def test_secret_map_is_memoised_per_path(tmp_path):
    path = _map_file(tmp_path)
    assert secret_map(str(path)) is secret_map(str(path))


def test_lookup_without_a_path_or_env_var_is_unavailable(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_IDENTITY_MAP", raising=False)
    policy = IdentityPolicy(
        surface="plane", mode="lookup", target="header", map={"x-api-key": "api_key"}
    )
    with pytest.raises(Unavailable, match="identity map"):
        policy.materialise(_user())

"""The kill-switch state file (spec §5.1, §5.2)."""

import json
import logging
import os
import stat

import pytest

from beherouter.errors import REASONS, Conflict, UsageError
from beherouter.killswitch import KillSwitch, configured, parse_state
from beherouter.outcomes import CONTEXT_KEYS


def test_reasons_and_context_key_exist():
    assert REASONS["surface_disabled"] == "refused"
    assert REASONS["caller_blocked"] == "refused"
    assert "scope" in CONTEXT_KEYS


def test_a_missing_file_is_empty_state(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    st = ks.state()
    assert st.all is None and st.surfaces == {} and st.subjects == {}
    assert st.disabled("x") is None


def test_parse_state_reads_all_three_kinds():
    st = parse_state({"all": {"reason": "r"}, "surfaces": {"dwh": {}}, "subjects": {"u": {}}})
    assert st.disabled("other") == "all"
    assert parse_state({"surfaces": {"dwh": {}}}).disabled("dwh") == "surface"
    assert "u" in st.subjects


@pytest.mark.parametrize(
    "bad",
    [[], {"nope": {}}, {"all": "yes"}, {"surfaces": []}, {"surfaces": {"a": "x"}},
     {"subjects": {"": {}}}],
)
def test_parse_state_refuses_a_bad_shape(bad):
    with pytest.raises(ValueError):
        parse_state(bad)


def test_state_is_reread_only_when_the_file_changes(tmp_path):
    p = tmp_path / "ks.json"
    p.write_text(json.dumps({"surfaces": {"a": {}}}))
    ks = KillSwitch(p)
    first = ks.state()
    assert ks.state() is first  # same stamp, same object
    p.write_text(json.dumps({"surfaces": {"a": {}, "b": {}}}))
    os.utime(p, ns=(1, 1))  # force a different stamp even on coarse clocks
    assert set(ks.state().surfaces) == {"a", "b"}


def test_a_malformed_file_keeps_last_good_and_goes_stale(tmp_path, caplog):
    p = tmp_path / "ks.json"
    p.write_text(json.dumps({"subjects": {"u-1": {}}}))
    ks = KillSwitch(p)
    assert "u-1" in ks.state().subjects
    p.write_text("{not json")
    os.utime(p, ns=(2, 2))
    with caplog.at_level(logging.ERROR, logger="beherouter.killswitch"):
        assert "u-1" in ks.state().subjects
        ks.state()  # same bad stamp: logged once
    assert ks.stale
    assert len([r for r in caplog.records if "malformed" in r.getMessage()]) == 1
    assert "u-1" not in caplog.text


def test_a_vanished_file_lifts_everything_with_one_warning(tmp_path, caplog):
    p = tmp_path / "ks.json"
    p.write_text(json.dumps({"all": {}, "surfaces": {"dwh": {}}, "subjects": {"u-1": {}}}))
    ks = KillSwitch(p)
    assert ks.state().subjects
    p.unlink()
    with caplog.at_level(logging.WARNING, logger="beherouter.killswitch"):
        assert ks.state().empty()
        ks.state()  # still gone: not warned again
    gone = [r for r in caplog.records if "is gone" in r.getMessage()]
    assert len(gone) == 1 and gone[0].levelno == logging.WARNING
    assert "1 subject(s)" in gone[0].getMessage()
    assert "u-1" not in caplog.text and "dwh" not in caplog.text
    # Once per transition: a new stop that vanishes again warns again.
    p.write_text(json.dumps({"surfaces": {"dwh": {}}}))
    assert ks.state().surfaces
    p.unlink()
    with caplog.at_level(logging.WARNING, logger="beherouter.killswitch"):
        ks.state()
    assert len([r for r in caplog.records if "is gone" in r.getMessage()]) == 2


def test_a_file_that_was_never_there_or_held_nothing_warns_nothing(tmp_path, caplog):
    p = tmp_path / "ks.json"
    ks = KillSwitch(p)
    with caplog.at_level(logging.WARNING, logger="beherouter.killswitch"):
        ks.state()
        p.write_text("{}")
        ks.state()
        p.unlink()
        ks.state()
    assert "is gone" not in caplog.text


def test_a_malformed_file_with_no_last_good_raises_usage_error(tmp_path):
    p = tmp_path / "ks.json"
    p.write_text("[]")
    with pytest.raises(UsageError, match="kill-switch"):
        KillSwitch(p).state()


async def test_update_writes_atomically_with_mode_0600(tmp_path):
    p = tmp_path / "ks.json"
    ks = KillSwitch(p)

    def block(raw):
        raw.setdefault("subjects", {})["u-1"] = {"reason": "offboarded"}

    st = await ks.update(block, actor="admin@x")
    assert "u-1" in st.subjects
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    on_disk = json.loads(p.read_text())
    assert on_disk["subjects"]["u-1"]["reason"] == "offboarded"
    assert list(tmp_path.iterdir()) == [p]  # no temp file left behind


async def test_update_keeps_a_hand_edit_made_between_writes(tmp_path):
    p = tmp_path / "ks.json"
    ks = KillSwitch(p)
    await ks.update(lambda raw: raw.setdefault("surfaces", {}).update(a={}), actor="x")
    data = json.loads(p.read_text())
    data["surfaces"]["hand"] = {}
    p.write_text(json.dumps(data))
    st = await ks.update(lambda raw: raw.setdefault("surfaces", {}).update(b={}), actor="x")
    assert set(st.surfaces) == {"a", "b", "hand"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
async def test_update_on_a_read_only_directory_is_a_conflict(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        ks = KillSwitch(d / "ks.json")
        with pytest.raises(Conflict, match="cannot be written"):
            await ks.update(lambda raw: raw.update(all={}), actor="x")
    finally:
        d.chmod(0o700)


async def test_update_refuses_to_overwrite_a_malformed_file(tmp_path):
    p = tmp_path / "ks.json"
    p.write_text("{oops")
    ks = KillSwitch(p)
    with pytest.raises(Conflict, match="malformed"):
        await ks.update(lambda raw: raw.update(all={}), actor="x")
    assert p.read_text() == "{oops"


def test_configured_is_none_when_unset_and_shared_per_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BEHEROUTER_KILLSWITCH_PATH", raising=False)
    assert configured() is None
    monkeypatch.setenv("BEHEROUTER_KILLSWITCH_PATH", str(tmp_path / "ks.json"))
    assert configured() is configured()


def test_a_stale_file_is_not_reparsed_on_every_call(tmp_path, monkeypatch):
    """While the file stays malformed, each call costs one stat, not a read
    and a parse plus a second stat."""
    p = tmp_path / "ks.json"
    p.write_text('{"surfaces": {"dwh": {}}}')
    ks = KillSwitch(p)
    ks.state()
    p.write_text("{not json")
    assert ks.state().disabled("dwh") == "surface" and ks.stale
    reads = []
    real = type(p).read_text
    monkeypatch.setattr(type(p), "read_text", lambda self, *a, **k: reads.append(1) or real(self))
    for _ in range(3):
        assert ks.state().disabled("dwh") == "surface"
    assert reads == [] and ks.stale


def test_an_unreadable_file_names_the_error_type_cleanly(tmp_path):
    p = tmp_path / "ks.json"
    p.mkdir()  # reading a directory is an OSError
    with pytest.raises(UsageError) as e:
        KillSwitch(p).state()
    assert "(IsADirectoryError)" in str(e.value)


def test_as_json_cannot_mutate_the_cached_state(tmp_path):
    p = tmp_path / "ks.json"
    p.write_text('{"surfaces": {"dwh": {"reason": "r"}}}')
    ks = KillSwitch(p)
    ks.state().as_json()["surfaces"]["office"] = {}
    ks.state().as_json()["surfaces"]["dwh"]["reason"] = "changed"
    assert ks.state().surfaces == {"dwh": {"reason": "r"}}


async def test_a_change_leaving_an_invalid_file_is_a_usage_error(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")

    def bad(raw):
        raw["surfaces"] = []

    with pytest.raises(UsageError, match="invalid"):
        await ks.update(bad, actor="a")
    assert not (tmp_path / "ks.json").exists()

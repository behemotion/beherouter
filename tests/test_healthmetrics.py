"""Deep-health records rendered for node_exporter's textfile collector.

Pure: records in, exposition text out. The CLI owns running the sweep and the
file write; this module owns the contract an alert rule is written against.
"""

import os

import pytest

from beherouter.healthmetrics import render, write_textfile

NOW = 1_760_000_000.0

GREEN = {
    "name": "office",
    "attach": "ok",
    "catalogue": "ok",
    "probe": "ok",
    "identity": {"mode": "none"},
}
DEAD = {"name": "ghost", "attach": "failed", "probe": "skipped", "error": "boom"}
UNPROBED = {"name": "quiet", "attach": "ok", "catalogue": "ok", "probe": "none"}
LOST_PINS = {
    "name": "plane",
    "attach": "ok",
    "catalogue": "pinned_missing",
    "pinned_missing": ["a", "b"],
    "probe": "ok",
}


def _samples(text: str) -> dict[str, float]:
    """`name{labels}` -> value, comments dropped."""
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            out[key] = float(value)
    return out


def test_a_green_surface():
    s = _samples(render([GREEN], now=NOW))
    assert s['beherouter_surface_attach_ok{surface="office"}'] == 1
    assert s['beherouter_surface_probe_ok{surface="office"}'] == 1
    assert s['beherouter_surface_probe_configured{surface="office"}'] == 1
    assert s['beherouter_surface_pinned_missing{surface="office"}'] == 0
    assert s['beherouter_surface_healthy{surface="office"}'] == 1
    assert s["beherouter_health_ok"] == 1
    assert s["beherouter_health_surfaces"] == 1
    assert s["beherouter_health_last_run_timestamp_seconds"] == NOW


def test_a_dead_attach_fails_the_surface_and_its_probe():
    s = _samples(render([DEAD], now=NOW))
    assert s['beherouter_surface_attach_ok{surface="ghost"}'] == 0
    # Skipped because attach failed: the credential was NOT proven, so 0.
    assert s['beherouter_surface_probe_ok{surface="ghost"}'] == 0
    assert s['beherouter_surface_healthy{surface="ghost"}'] == 0
    assert s["beherouter_health_ok"] == 0
    # No catalogue was seen: absent, not a fabricated 0.
    assert 'beherouter_surface_pinned_missing{surface="ghost"}' not in s


def test_an_unprobed_surface_has_no_probe_ok_series():
    """`none` is absence of evidence. A 0 would page forever; a 1 would claim a
    credential nobody checked. Absent, with `probe_configured` 0 beside it."""
    s = _samples(render([UNPROBED], now=NOW))
    assert 'beherouter_surface_probe_ok{surface="quiet"}' not in s
    assert s['beherouter_surface_probe_configured{surface="quiet"}'] == 0
    assert s['beherouter_surface_healthy{surface="quiet"}'] == 1


def test_lost_pins_are_counted():
    s = _samples(render([LOST_PINS], now=NOW))
    assert s['beherouter_surface_pinned_missing{surface="plane"}'] == 2
    assert s['beherouter_surface_healthy{surface="plane"}'] == 0


def test_no_user_probe_series_without_a_bearer():
    assert "user_probe" not in render([GREEN], now=NOW)


@pytest.mark.parametrize(
    "state, ok",
    [("ok", 1), ("mismatch", 0), ("failed", 0), ("rejected", 0), ("refused", 0)],
)
def test_the_user_probe_verdict(state, ok):
    record = {**GREEN, "user_probe": {"state": state, "subject": "alice@example.com"}}
    s = _samples(render([record], now=NOW))
    assert s['beherouter_surface_user_probe_ok{surface="office"}'] == ok
    assert s[f'beherouter_surface_user_probe_state{{surface="office",state="{state}"}}'] == 1


@pytest.mark.parametrize("state", ["not_applicable", "skipped"])
def test_an_inapplicable_user_probe_has_no_ok_series(state):
    record = {**GREEN, "user_probe": {"state": state}}
    s = _samples(render([record], now=NOW))
    assert 'beherouter_surface_user_probe_ok{surface="office"}' not in s
    assert s[f'beherouter_surface_user_probe_state{{surface="office",state="{state}"}}'] == 1


def test_no_identity_value_ever_reaches_the_file():
    """⚠️ A textfile is world-readable on most hosts and scraped into a TSDB that
    keeps it for months. Subject, backend identity and error text stay out."""
    record = {
        **GREEN,
        "error": "token for bob@example.com expired",
        "user_probe": {
            "state": "mismatch",
            "subject": "alice@example.com",
            "backend_identity": {"email": "bob@example.com", "id": 42},
            "matches_caller": False,
        },
    }
    text = render([record], now=NOW)
    assert "example.com" not in text
    assert "42" not in text.replace("beherouter", "")  # not the id either


def test_label_values_are_escaped():
    record = {**GREEN, "name": 'we"ird\\na\nme'}
    text = render([record], now=NOW)
    assert 'surface="we\\"ird\\\\na\\nme"' in text


def test_every_family_has_help_and_type():
    text = render([GREEN, DEAD, {**GREEN, "name": "u", "user_probe": {"state": "ok"}}], now=NOW)
    families = {
        line.split(" ", 1)[0].split("{", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }
    for family in families:
        assert f"# HELP {family} " in text
        assert f"# TYPE {family} gauge" in text
    assert text.endswith("\n")


def test_an_empty_registry_is_healthy():
    s = _samples(render([], now=NOW))
    assert s["beherouter_health_ok"] == 1
    assert s["beherouter_health_surfaces"] == 0


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "beherouter.prom"
    target.write_text("old\n")
    write_textfile(target, "new\n")
    assert target.read_text() == "new\n"
    assert os.listdir(tmp_path) == ["beherouter.prom"]


def test_write_is_readable_by_the_collector(tmp_path):
    """mkstemp creates 0600; node_exporter usually runs as another user."""
    target = tmp_path / "beherouter.prom"
    write_textfile(target, "x 1\n")
    assert target.stat().st_mode & 0o044 == 0o044


def test_write_temp_file_does_not_end_in_prom(tmp_path, monkeypatch):
    """The collector reads every *.prom in the directory; a half-written temp
    file with that suffix would be scraped mid-write."""
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_textfile(tmp_path / "beherouter.prom", "x 1\n")
    assert seen and not seen[0].endswith(".prom")
    assert os.path.dirname(seen[0]) == str(tmp_path)  # same fs: rename is atomic

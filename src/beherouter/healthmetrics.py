"""Deep-health records as a node_exporter textfile-collector file.

Why this exists: Prometheus only ever probed the shallow `/healthz`, and
`/healthz` stays green through a revoked credential — `tools/list` is served
from the attach-time catalogue (the 2026-07-30 incident). Only `health --deep`
touches a credential, and until now nothing ran it on a schedule. A timer runs
`beherouter health --deep --textfile PATH`; node_exporter's textfile collector
exports the file; alert rules are written against the names below. That
contract is this module; contrib/health-textfile/ carries the scheduling.

Pure rendering plus one atomic write, so the contract is unit-testable without
attaching anything.

⚠️ No identity value ever reaches this file — not a subject, not the backend's
answer, not an error string (errors can quote a token's claims). A textfile is
world-readable on most hosts and lands in a TSDB kept for months. Labels are
the surface name and, for the per-user probe, the verdict enum; nothing else.
"""

import contextlib
import os
import tempfile
from pathlib import Path

PREFIX = "beherouter"

# (name, help) — the order here is the order in the file.
_FAMILIES = {
    "surface_attach_ok": "1 if the surface's backend attached in this sweep, else 0.",
    "surface_probe_configured": "1 if the surface has a credential probe, else 0.",
    "surface_probe_ok": (
        "1 if the credential probe succeeded, 0 if it failed or attach failed; "
        "absent when no probe is configured."
    ),
    "surface_pinned_missing": (
        "Number of pinned tools the backend no longer serves; absent when the "
        "catalogue was not seen."
    ),
    "surface_user_probe_ok": (
        "1 if the probe run as the --bearer-file user reached the backend as that "
        "user, 0 if it was rejected, refused, failed or answered as someone else; "
        "absent when not applicable."
    ),
    "surface_user_probe_state": "1 for the per-user probe's verdict (label state).",
    "surface_healthy": "1 unless the surface is in the sweep's failed list.",
    "health_ok": "1 if no surface failed this sweep, else 0.",
    "health_surfaces": "Surfaces checked in this sweep.",
    "health_last_run_timestamp_seconds": (
        "Unix time the sweep finished. Alert when stale: a sweep that cannot run "
        "at all (unreadable registry, missing binary) writes nothing."
    ),
}

# Verdicts that prove or disprove the per-user path. `not_applicable` and
# `skipped` prove nothing either way, so — exactly like an unprobed surface —
# they get no `_ok` series rather than a 0 that would page forever.
_USER_OK = {"ok": 1}
_USER_BAD = {"rejected", "refused", "failed", "mismatch"}


def _escape(value: str) -> str:
    """A label value per the exposition format: backslash, quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    return "{" + ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items()) + "}"


def _num(value: float) -> str:
    return repr(float(value)) if isinstance(value, float) and not value.is_integer() else (
        str(int(value))
    )


def render(records: list[dict], now: float, failed_names: list[str] | None = None) -> str:
    """The textfile for one sweep's records.

    `failed_names` is `health.failed(records)` — passed in by the caller so the
    verdict here is the SAME verdict the exit code carries, decided in one
    place; computed from `health` when omitted.
    """
    if failed_names is None:
        from .health import failed

        failed_names = failed(records)
    bad = set(failed_names)
    samples: dict[str, list[str]] = {k: [] for k in _FAMILIES}

    def add(family: str, value, **labels: str) -> None:
        samples[family].append(
            f"{PREFIX}_{family}{_labels(**labels) if labels else ''} {_num(value)}"
        )

    for r in records:
        s = r["name"]
        attached = r.get("attach") == "ok"
        add("surface_attach_ok", int(attached), surface=s)
        probe = r.get("probe")
        add("surface_probe_configured", int(probe != "none"), surface=s)
        if probe != "none":
            add("surface_probe_ok", int(probe == "ok"), surface=s)
        if "catalogue" in r:
            add("surface_pinned_missing", len(r.get("pinned_missing") or []), surface=s)
        user = r.get("user_probe")
        if isinstance(user, dict) and user.get("state"):
            state = str(user["state"])
            if state in _USER_OK or state in _USER_BAD:
                add("surface_user_probe_ok", int(state in _USER_OK), surface=s)
            add("surface_user_probe_state", 1, surface=s, state=state)
        add("surface_healthy", int(s not in bad), surface=s)
    add("health_ok", int(not bad))
    add("health_surfaces", len(records))
    add("health_last_run_timestamp_seconds", now)

    lines: list[str] = []
    for family, help_ in _FAMILIES.items():
        if not samples[family]:
            continue
        lines.append(f"# HELP {PREFIX}_{family} {help_}")
        lines.append(f"# TYPE {PREFIX}_{family} gauge")
        lines.extend(samples[family])
    return "\n".join(lines) + "\n"


def write_textfile(path: Path | str, text: str) -> None:
    """Replace `path` atomically: temp file in the SAME directory, then rename.

    ⚠️ The collector reads every `*.prom` in its directory on every scrape, so
    an in-place write can be scraped half-done — a truncated file silently
    drops series, and an absent series does not fire a `== 0` alert. The temp
    name therefore must not end in `.prom`, and it must live on the same
    filesystem or the rename is not atomic.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp makes the file 0600; node_exporter typically runs as its own
        # user and would then read nothing, silently.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise

"""The recorded calendar catalogue: `gcal` and `m365` maturity evidence.

Both plugins are native, so their `tools/list` is the descriptor set the
shared core in `plugins/calendar/tools.py` builds — there is no upstream server
to record from. This module is the recorder AND the drift guard: the rows are
generated from the REAL plugin's `build` (dummy credentials; attach performs no
network I/O), and the test fails the moment the recorded file and the live
definitions disagree.

Re-record after a deliberate schema change, then re-run the suite:

    BEHEROUTER_RECORD_CATALOGUE=1 uv run pytest tests/test_calendar_catalogue.py
"""

import json
import os
from pathlib import Path

import pytest

from beherouter.plugins import PLUGINS, get
from beherouter.plugins.spec import PluginContext
from beherouter.testing import plugin_conformance

REPO = Path(__file__).resolve().parents[1]
# Versioned by the beherouter release whose tool definitions it records.
CATALOGUE = REPO / "tests/fixtures/catalogues/calendar-0.2.5.json"
ENV = {"client_id": "id", "client_secret": "secret", "refresh_token": "rt"}


async def _rows(name: str) -> list[dict]:
    """The plugin's tool definitions, in `scripts/record_catalogue.py`'s row shape."""
    spec = get(name).spec
    ctx = PluginContext(
        surface=name,
        config={f.name: f.default for f in spec.config},
        env={v.name: ENV[v.name] for v in spec.env},
        pinned=list(spec.pinned),
    )
    backend = await get(name).build(ctx)
    return [
        {
            "name": d.name,
            "description": d.summary,
            "inputSchema": d.schema,
            "annotations": d.annotations or None,
        }
        for d in sorted(backend.descriptors, key=lambda d: d.name)
    ]


def _dump(rows: list[dict]) -> str:
    return json.dumps(rows, indent=1, sort_keys=True) + "\n"


async def test_the_recorded_catalogue_matches_both_plugins():
    gcal, m365 = await _rows("gcal"), await _rows("m365")
    # One file serves both because the two expose byte-identical tool schemas.
    assert _dump(gcal) == _dump(m365)
    if os.environ.get("BEHEROUTER_RECORD_CATALOGUE"):
        CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
        CATALOGUE.write_text(_dump(gcal))
    assert CATALOGUE.read_text() == _dump(gcal), (
        "calendar tool definitions drifted from the recorded catalogue; "
        "re-record (see this module's docstring) and bump its version"
    )


@pytest.mark.parametrize("name", ["gcal", "m365"])
def test_the_calendar_plugins_cite_the_catalogue_and_meet_catalogued(name):
    spec = PLUGINS[name].spec
    assert str(CATALOGUE.relative_to(REPO)) in spec.evidence
    report = plugin_conformance(spec, root=REPO)
    assert report.ok and report.met == "catalogued", report

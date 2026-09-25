"""Search quality gates against real, recorded backend catalogues.

The catalogue is a snapshot, not a mock: re-record it with
scripts/record_catalogue.py after upgrading the backend.
"""


def test_plane_catalogue_fixture_loads(catalogue_descriptors):
    ds = catalogue_descriptors("plane-0.3.2")
    assert len(ds) == 30
    assert {"workitem", "cycle", "state", "member"} <= {d.name for d in ds}

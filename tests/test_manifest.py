import pytest

from beherouter.errors import Unavailable
from beherouter.manifest import load_schema, validate_manifest

VALID = {
    "tool": "demo",
    "version": "0.1.0",
    "summary": "x",
    "verbs": [
        {"name": "search", "summary": "s", "args": [], "pinned": True, "mutating": False}
    ],
}


def test_schema_loads():
    schema = load_schema()
    assert schema["required"] == ["tool", "version", "summary", "verbs"]


def test_valid_manifest_passes():
    validate_manifest(VALID)  # no raise


def test_invalid_manifest_raises_unavailable():
    bad = {"tool": "demo"}  # missing required keys
    with pytest.raises(Unavailable):
        validate_manifest(bad)


def test_extra_key_is_rejected():
    """The contract is closed (additionalProperties: false at every level).

    A sibling emitting an extra key fails attach with exit 6 — intentional, per
    HARNESS-DIVERGENCES #4. Pinned here so nobody 'helpfully' loosens the schema.
    """
    extra = {**VALID, "extra_key": "nope"}
    with pytest.raises(Unavailable):
        validate_manifest(extra)


def test_bad_arg_type_is_rejected():
    bad = {
        **VALID,
        "verbs": [
            {
                "name": "search",
                "summary": "s",
                "args": [{"name": "q", "type": "object", "required": True}],
                "pinned": True,
                "mutating": False,
            }
        ],
    }
    with pytest.raises(Unavailable):
        validate_manifest(bad)

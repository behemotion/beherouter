import pytest

from beherouter.args import prepare_args
from beherouter.errors import UsageError
from beherouter.models import ToolDescriptor

CLOSED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["list", "create", "archive"]},
        "project_id": {"type": "string"},
        "archive": {"type": "boolean", "default": True},
    },
}
OPEN = {**CLOSED, "additionalProperties": True}
CLI = {
    "name": {"name": "name", "type": "string", "required": True},
    "--dry-run": {"name": "--dry-run", "type": "boolean", "required": False},
}


def _d(schema, name="workitem"):
    return ToolDescriptor(name=name, verb=name, summary="", schema=schema,
                          pinned=False, mutating=None)


def test_drops_none_and_default_echoes():
    """The archive=True-on-create failure, now on the run_tool path too."""
    got = prepare_args(_d(CLOSED), {"action": "create", "archive": True, "project_id": None})
    assert got == {"action": "create"}


def test_maps_param_spelling_to_wire_name():
    assert prepare_args(_d(CLI), {"name": "x", "dry_run": True}) == {
        "name": "x", "--dry-run": True,
    }


def test_accepts_wire_spelling():
    assert prepare_args(_d(CLI), {"name": "x", "--dry-run": True}) == {
        "name": "x", "--dry-run": True,
    }


def test_refuses_both_spellings_of_one_arg():
    with pytest.raises(UsageError, match="both"):
        prepare_args(_d(CLI), {"name": "x", "dry_run": True, "--dry-run": False})


def test_missing_required_lists_the_enum():
    with pytest.raises(UsageError, match=r"missing required arg 'action' \(one of: list, create, archive\)"):
        prepare_args(_d(CLOSED), {"project_id": "p"})


def test_unknown_arg_on_closed_schema_suggests():
    with pytest.raises(UsageError, match="unknown arg 'projectId'; did you mean 'project_id'"):
        prepare_args(_d(CLOSED), {"action": "list", "projectId": "p"})


def test_unknown_arg_on_open_schema_is_forwarded():
    assert prepare_args(_d(OPEN), {"action": "list", "extra": 1}) == {
        "action": "list", "extra": 1,
    }


def test_cli_schema_is_closed():
    with pytest.raises(UsageError, match="unknown arg 'colour'"):
        prepare_args(_d(CLI), {"name": "x", "colour": "red"})


def test_empty_schema_takes_no_args():
    assert prepare_args(_d({}), {}) == {}

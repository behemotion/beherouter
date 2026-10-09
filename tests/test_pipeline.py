"""The ONE call path (spec §2)."""

import json
import logging

import pytest
from fastmcp import Client

from beherouter import metrics
from beherouter.audit import AuditSink
from beherouter.errors import Unavailable, UsageError
from beherouter.models import Backend, ToolDescriptor
from beherouter.outcomes import META_KEY
from beherouter.surface import build_surface


class Recording:
    """An executor that answers, rejects or fails on demand."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls = []

    async def run(self, verb, args, *, identity=None):
        self.calls.append((verb, args))
        if self.behaviour == "reject":
            raise UsageError(f"backend rejected '{verb}': type_id is not a valid UUID")
        if self.behaviour == "down":
            raise Unavailable(f"backend call '{verb}' failed: connection refused")
        if self.behaviour == "bug":
            raise RuntimeError("secret internal detail")
        return {"result": {"ok": True}}


def _descriptor(name, pinned=True):
    return ToolDescriptor(
        name=name, verb=name, summary=f"{name} things",
        schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        pinned=pinned, mutating=False,
    )


def _surface(behaviour="ok", lines=None):
    ex = Recording(behaviour)
    backend = Backend(
        name="sx", kind="mcp", executor=ex,
        descriptors=[_descriptor("find"), _descriptor("hidden_tool", pinned=False)],
    )
    sink = AuditSink(write=(lines.append if lines is not None else lambda _l: None))
    return build_surface(backend, audit=sink), ex


async def test_ok_pinned_call_writes_one_audit_line():
    lines: list[str] = []
    surface, _ = _surface(lines=lines)
    async with Client(surface) as c:
        await c.call_tool("find", {"q": "secret-argument-value"})
    [line] = lines
    body = json.loads(line)
    assert (body["surface"], body["tool"], body["inner_tool"], body["outcome"]) == (
        "sx", "find", None, "ok",
    )
    assert "secret-argument-value" not in line


@pytest.mark.parametrize(("audit_on", "level"), [(True, "DEBUG"), (False, "INFO")])
async def test_an_ok_call_logs_at_debug_only_while_the_audit_records_it(caplog, audit_on, level):
    """With the audit on, the audit line is the record of an OK call and the
    calls line would only double the volume; with it off, the calls line is
    the only trace left at the default level."""
    caplog.set_level(logging.DEBUG)
    backend = Backend(
        name="sx", kind="mcp", executor=Recording(), descriptors=[_descriptor("find")],
    )
    surface = build_surface(backend, audit=AuditSink(enabled=audit_on, write=lambda _l: None))
    async with Client(surface) as c:
        await c.call_tool("find", {"q": "x"})
    ours = [r for r in caplog.records if r.name == "beherouter.calls"]
    assert [r.levelname for r in ours] == [level]


async def test_run_tool_is_labelled_with_its_inner_tool():
    lines: list[str] = []
    surface, _ = _surface(lines=lines)
    before = metrics.REGISTRY.get_sample_value(
        "beherouter_tool_calls_total", {"surface": "sx", "tool": "hidden_tool", "outcome": "ok"}
    ) or 0
    async with Client(surface) as c:
        await c.call_tool("run_tool", {"name": "hidden_tool", "args": {"q": "x"}})
    assert json.loads(lines[0])["inner_tool"] == "hidden_tool"
    assert metrics.REGISTRY.get_sample_value(
        "beherouter_tool_calls_total", {"surface": "sx", "tool": "hidden_tool", "outcome": "ok"}
    ) == before + 1


async def test_unknown_run_tool_name_is_never_a_label():
    lines: list[str] = []
    surface, _ = _surface(lines=lines)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "caller-chosen-name", "args": {}})
    assert res.isError
    assert res.meta[META_KEY]["reason"] == "unknown_tool"
    body = json.loads(lines[0])
    assert body["inner_tool"] == "<unknown>" and "caller-chosen-name" not in lines[0]


async def test_bad_arguments_are_classified_before_the_backend():
    surface, ex = _surface()
    async with Client(surface) as c:
        res = await c.call_tool_mcp("run_tool", {"name": "hidden_tool", "args": {"nope": 1}})
    assert res.meta[META_KEY]["reason"] == "bad_arguments"
    assert ex.calls == []


@pytest.mark.parametrize(
    ("behaviour", "reason", "level"),
    [("reject", "backend_rejected", "WARNING"), ("down", "backend_unavailable", "ERROR")],
)
async def test_backend_failures_log_one_line_without_traceback(caplog, behaviour, reason, level):
    caplog.set_level(logging.INFO)
    surface, _ = _surface(behaviour)
    async with Client(surface) as c:
        res = await c.call_tool_mcp("find", {"q": "x"})
    assert res.isError and res.meta[META_KEY]["reason"] == reason
    ours = [r for r in caplog.records if r.name == "beherouter.calls"]
    assert [r.levelname for r in ours] == [level]
    assert all(r.exc_info is None for r in caplog.records)
    assert not [r for r in caplog.records if "Error calling tool" in r.getMessage()]


async def test_an_internal_bug_is_logged_once_with_its_traceback(caplog):
    surface, _ = _surface("bug")
    async with Client(surface) as c:
        res = await c.call_tool_mcp("find", {"q": "x"})
    assert res.meta[META_KEY]["reason"] == "internal"
    assert "secret internal detail" not in res.content[0].text
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1 and tracebacks[0].name == "beherouter.calls"


async def test_meta_tools_are_audited_too():
    lines: list[str] = []
    surface, _ = _surface(lines=lines)
    async with Client(surface) as c:
        await c.call_tool("search_tools", {"query": "things"})
        await c.call_tool("describe_tool", {"name": "find"})
    assert [json.loads(line)["tool"] for line in lines] == ["search_tools", "describe_tool"]


async def test_a_caller_without_a_token_gets_unauthenticated_meta():
    from beherouter.identity import IdentityPolicy

    policy = IdentityPolicy(surface="sx", require_roles=("r",), roles_claim="roles")
    backend = Backend(
        name="sx", kind="mcp", executor=Recording(), descriptors=[_descriptor("find")]
    )
    surface = build_surface(backend, policy=policy, audit=AuditSink(enabled=False))
    async with Client(surface) as c:  # in-process: no token at all
        res = await c.call_tool_mcp("find", {"q": "x"})
    meta = res.meta[META_KEY]
    assert meta["type"] == "auth" and meta["reason"] == "unauthenticated"


async def test_caller_chosen_names_never_reach_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    surface, _ = _surface()
    async with Client(surface) as c:
        await c.call_tool_mcp("run_tool", {"name": "caller-chosen-name", "args": {}})
        await c.call_tool_mcp(
            "run_tool", {"name": "hidden_tool", "args": {"nope-secret-key": 1}}
        )
    ours = [r for r in caplog.records if r.name == "beherouter.calls"]
    assert [r.levelname for r in ours] == ["WARNING", "WARNING"]
    for r in caplog.records:
        text = r.getMessage() + str(getattr(r, "fields", ""))
        assert "caller-chosen-name" not in text and "nope-secret-key" not in text


# --- refused by FastMCP before the tool function runs (schema validation, an
# unpublished name): still one audited, counted, classified call ---------------

SCHEMA_SECRET = "SECRET-INPUT-VALUE-7f3a"


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def every_log_record(caplog):
    """caplog sees only what propagates to the root; FastMCP's own logger may
    not. A handler on it sees its records AFTER its filters, as a deployment's
    handler would. At INFO, the level the gateway runs at (logsetup)."""
    caplog.set_level(logging.INFO)
    collect = _Collect()
    fastmcp_server = logging.getLogger("fastmcp.server.server")
    fastmcp_server.addHandler(collect)
    yield lambda: caplog.records + collect.records
    fastmcp_server.removeHandler(collect)


def _calls(tool, outcome):
    labels = {"surface": "sx", "tool": tool, "outcome": outcome}
    return metrics.REGISTRY.get_sample_value("beherouter_tool_calls_total", labels) or 0


@pytest.mark.parametrize(
    ("tool", "arguments", "label"),
    [
        ("find", {"q": {"leak": SCHEMA_SECRET}}, "find"),
        ("run_tool", {"name": "hidden_tool", "args": f'{{"q": "{SCHEMA_SECRET}"}}'}, "run_tool"),
    ],
)
async def test_schema_invalid_arguments_are_one_audited_call(
    every_log_record, tool, arguments, label
):
    lines: list[str] = []
    surface, ex = _surface(lines=lines)
    before = _calls(label, "tool_error")
    async with Client(surface) as c:
        res = await c.call_tool_mcp(tool, arguments)
    assert res.isError and res.meta[META_KEY]["reason"] == "bad_arguments"
    [line] = lines
    body = json.loads(line)
    assert (body["tool"], body["outcome"], body["reason"]) == (label, "tool_error", "bad_arguments")
    assert _calls(label, "tool_error") == before + 1
    assert ex.calls == []
    text = res.content[0].text
    assert SCHEMA_SECRET not in text and ("q" in text or "args" in text)
    for r in every_log_record():
        assert SCHEMA_SECRET not in r.getMessage() + str(getattr(r, "fields", "")), r.name


async def test_an_unpublished_name_is_one_audited_call_never_a_label(every_log_record):
    lines: list[str] = []
    surface, ex = _surface(lines=lines)
    before = _calls(metrics.UNKNOWN_TOOL, "not_found")
    async with Client(surface) as c:
        res = await c.call_tool_mcp("caller-invented-tool", {"q": "x"})
    assert res.isError and res.meta[META_KEY]["reason"] == "unknown_tool"
    [line] = lines
    body = json.loads(line)
    assert (body["tool"], body["reason"]) == (metrics.UNKNOWN_TOOL, "unknown_tool")
    assert "caller-invented-tool" not in line
    assert _calls(metrics.UNKNOWN_TOOL, "not_found") == before + 1
    assert ex.calls == []
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            assert "caller-invented-tool" not in sample.labels.values()
    for r in every_log_record():
        assert "caller-invented-tool" not in r.getMessage() + str(getattr(r, "fields", ""))


async def test_a_call_the_pipeline_saw_is_not_recorded_twice():
    """The middleware catches only FastMCP's PRE-call refusals; a tool function
    never raises (the pipeline returns its errors), so no call is counted twice."""
    lines: list[str] = []
    surface, _ = _surface("reject", lines=lines)
    async with Client(surface) as c:
        await c.call_tool_mcp("find", {"q": "x"})
        await c.call_tool_mcp("run_tool", {"name": "find", "args": {"q": "x"}})
    assert len(lines) == 2

import json
import logging
import time

import pytest

from beherouter import logsetup
from beherouter.errors import UsageError


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.fixture(autouse=True)
def _restore_root():
    """configure() rewires process-wide loggers; put them back for later tests."""
    names = (
        "",
        "fastmcp",
        "mcp",
        "httpx",
        "httpcore",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "beherouter.audit",
    )
    saved = {
        n: (
            logging.getLogger(n).handlers[:],
            logging.getLogger(n).propagate,
            logging.getLogger(n).level,
        )
        for n in names
    }
    yield
    for n, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def test_timestamp_carries_the_local_offset(berlin):
    # 2026-01-15 12:00:00 UTC is 13:00 in Berlin (CET, +01:00).
    assert logsetup.timestamp(1768478400.0) == "2026-01-15T13:00:00.000+01:00"


def test_json_lines_parse_and_carry_fields():
    rec = logging.LogRecord(
        "beherouter.calls", logging.WARNING, __file__, 1, "call %s", ("x",), None
    )
    rec.fields = {"surface": "dwh", "outcome": "tool_error"}
    line = logsetup.JsonFormatter().format(rec)
    body = json.loads(line)
    assert body["level"] == "WARNING" and body["msg"] == "call x"
    assert body["surface"] == "dwh" and body["outcome"] == "tool_error"
    assert "\n" not in line


def test_json_puts_a_traceback_on_one_line():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    line = logsetup.JsonFormatter().format(rec)
    assert "\n" not in line
    assert "RuntimeError: boom" in json.loads(line)["exc"]


def test_bad_format_is_refused(monkeypatch):
    monkeypatch.setenv("BEHEROUTER_LOG_FORMAT", "xml")
    with pytest.raises(UsageError, match="BEHEROUTER_LOG_FORMAT"):
        logsetup.log_format()


def test_configure_removes_rich_handlers(monkeypatch):
    import fastmcp  # noqa: F401  (installs its RichHandler on import)
    from rich.logging import RichHandler

    monkeypatch.setenv("BEHEROUTER_LOG_FORMAT", "json")
    logsetup.configure()
    for name in ("", "fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert not any(isinstance(h, RichHandler) for h in lg.handlers), name
    assert isinstance(logging.getLogger().handlers[0].formatter, logsetup.JsonFormatter)
    assert logging.getLogger("fastmcp").propagate is True


def test_configure_routes_audit_to_its_own_handler(monkeypatch):
    monkeypatch.delenv("BEHEROUTER_LOG_FORMAT", raising=False)
    logsetup.configure()
    audit_logger = logging.getLogger("beherouter.audit")
    assert audit_logger.propagate is False and len(audit_logger.handlers) == 1


def test_configure_keeps_third_party_info_quiet(monkeypatch):
    """httpx logs every request's full URL at INFO (calendar ids, openapi
    query arguments) and the MCP SDK logs session ids: none of it may reach
    the gateway log once the root is at INFO."""
    monkeypatch.delenv("BEHEROUTER_LOG_FORMAT", raising=False)
    logging.getLogger("mcp").addHandler(logging.NullHandler())
    logsetup.configure()
    for name in ("httpx", "httpcore", "mcp"):
        lg = logging.getLogger(name)
        assert lg.level == logging.WARNING, name
        assert lg.propagate is True, name
        assert not lg.isEnabledFor(logging.INFO), name
    assert logging.getLogger("mcp").handlers == []
    assert logging.getLogger("beherouter.calls").isEnabledFor(logging.INFO)

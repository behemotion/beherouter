"""The gateway's log output: one line per record, as text or JSON.

FastMCP installs Rich handlers of its own, which render tracebacks over many
lines — and multi-line records break log shipping. `configure()` takes the
root, FastMCP's and uvicorn's loggers over with one plain handler.
Timestamps carry the LOCAL offset, so `TZ` is honoured.
"""

import json
import logging
import os
import sys
from datetime import datetime

from .errors import UsageError

FORMAT_VAR = "BEHEROUTER_LOG_FORMAT"
FORMATS = ("text", "json")
_TAKEN_OVER = ("fastmcp", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access")
# Quiet below WARNING: with the root at INFO, httpx logs every request's FULL
# URL (calendar ids, openapi path and query arguments, a credential embedded in
# a backend URL) and the MCP SDK logs session ids — caller values the gateway
# must never log. They still propagate, so a warning or error reaches the log.
_QUIET = ("httpx", "httpcore", "mcp")


def timestamp(created: float) -> str:
    return datetime.fromtimestamp(created).astimezone().isoformat(timespec="milliseconds")


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatTime(self, record, datefmt=None):
        return timestamp(record.created)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "ts": timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            body.update({k: v for k, v in fields.items() if k not in body})
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)


def log_format() -> str:
    raw = os.environ.get(FORMAT_VAR, "").strip().lower() or "text"
    if raw not in FORMATS:
        raise UsageError(f"${FORMAT_VAR} must be one of {list(FORMATS)}, got {raw!r}")
    return raw


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format() == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in _TAKEN_OVER:
        taken = logging.getLogger(name)
        taken.handlers = []
        taken.propagate = True
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    from .audit import install_handler
    install_handler()

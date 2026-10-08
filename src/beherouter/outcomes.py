"""How one call ended — classified ONCE, read by everything that reports it.

The audit line, the metrics, the log line and the error `_meta` all read the
same `Outcome`, so they cannot disagree about what happened.

Errors are returned as `ToolResult(is_error=True)` rather than raised: FastMCP
3.4.5 logs any non-`FastMCPError` raised from a tool with `logger.exception`
(a multi-line Rich traceback) BEFORE middleware sees it, which is how an
ordinary backend 400 became an ERROR traceback in production logs.
"""

from dataclasses import dataclass
from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import TextContent

from .errors import REASONS, AuthError, AxiError, ExitCode, NotFound, Unavailable, UsageError

META_KEY = "io.beherouter/error"

# The phase a call was in when it failed. An UNTAGGED error is read by its
# phase: an AuthError while gating is the caller's; one from the backend is a
# backend refusal.
GATE, PREPARE, EXECUTE = "gate", "prepare", "execute"

# What `context` may carry to a client. An allow-list, so a key added to some
# raiser later cannot leak a caller value by accident.
CONTEXT_KEYS = frozenset(
    {"required_roles", "missing_roles", "expected_audience", "suggestions", "status", "limit_s"}
)


@dataclass(frozen=True)
class Outcome:
    kind: str
    reason: str | None = None
    status: int | None = None


OK = Outcome("ok")


def _untagged(exc: AxiError, phase: str) -> str:
    if phase == GATE:
        return "unauthenticated" if isinstance(exc, AuthError) else "identity_unavailable"
    if phase == PREPARE:
        if isinstance(exc, NotFound):
            return "unknown_tool"
        if isinstance(exc, UsageError):
            return "bad_arguments"
        return "backend_unavailable"
    if isinstance(exc, Unavailable):
        return "backend_unavailable"
    if isinstance(exc, (UsageError, NotFound, AuthError)):
        return "backend_rejected"
    return "internal"


def classify(exc: BaseException, phase: str) -> Outcome:
    if not isinstance(exc, AxiError):
        return Outcome("internal", "internal")
    reason = exc.context.get("reason")
    if reason not in REASONS:
        reason = _untagged(exc, phase)
    status = exc.context.get("status")
    return Outcome(
        REASONS[reason],
        reason,
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def error_result(exc: BaseException, outcome: Outcome) -> ToolResult:
    if isinstance(exc, AxiError):
        envelope = exc.envelope()["error"]
        text = str(exc)
        payload: dict[str, Any] = {
            "type": envelope["type"],
            "code": envelope["code"],
            "reason": outcome.reason,
        }
        context = {k: v for k, v in exc.context.items() if k in CONTEXT_KEYS}
        if context:
            payload["context"] = context
    else:
        # Our bug. The class only: an exception message can carry anything.
        text = f"internal error in the gateway ({type(exc).__name__}); see the gateway log"
        payload = {"type": "internal", "code": int(ExitCode.INTERNAL), "reason": "internal"}
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        meta={META_KEY: payload},
        is_error=True,
    )

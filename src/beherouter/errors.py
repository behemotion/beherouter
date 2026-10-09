"""Canonical error types — re-exported from beheaxi so beherouter speaks one envelope."""

from typing import Any

from beheaxi import AuthError, AxiError, Conflict, ExitCode, NotFound, Unavailable, UsageError

__all__ = [
    "REASONS",
    "AuthError",
    "AxiError",
    "Conflict",
    "ExitCode",
    "NotFound",
    "Unavailable",
    "UsageError",
    "tag",
]

# A failed call's stable reason, and the outcome kind it belongs to. The ONE
# vocabulary for the error `_meta`, the audit line, the metrics and the log
# line (docs/superpowers/specs/2026-10-08-call-pipeline-observability-design.md
# §3). Lives here, not in outcomes.py, because identity.py and plugins tag
# errors and must not import FastMCP to do it.
REASONS: dict[str, str] = {
    "unauthenticated": "refused",
    "missing_role": "refused",
    "wrong_audience": "refused",
    "rate_limited": "refused",
    "confirmation_required": "refused",
    "identity_unavailable": "unavailable",
    "unknown_tool": "not_found",
    "bad_arguments": "tool_error",
    "edition_unsupported": "tool_error",
    "backend_rejected": "tool_error",
    "backend_unavailable": "unavailable",
    "timeout": "timeout",
    "internal": "internal",
}


def tag(exc: AxiError, reason: str, **context: Any) -> AxiError:
    """Give an error a stable reason (and named context); returns it, for
    `raise tag(...)`. The FIRST tag sticks: the innermost raiser knows best.

    ⚠️ `context` is client-visible. Put what was REQUIRED in it, never what
    the caller had or sent.
    """
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}; known: {sorted(REASONS)}")
    exc.context.setdefault("reason", reason)
    for key, value in context.items():
        exc.context.setdefault(key, value)
    return exc

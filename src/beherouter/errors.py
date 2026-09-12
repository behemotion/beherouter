"""Canonical error types — re-exported from beheaxi so beherouter speaks one envelope."""

from beheaxi import AuthError, AxiError, Conflict, ExitCode, NotFound, Unavailable, UsageError

__all__ = [
    "AuthError",
    "AxiError",
    "Conflict",
    "ExitCode",
    "NotFound",
    "Unavailable",
    "UsageError",
]

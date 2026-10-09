"""The per-call audit line: who called which tool on which surface, and how
it ended. One JSON object per call on the `beherouter.audit` logger, written to
stdout as JSON whatever BEHEROUTER_LOG_FORMAT says — a SIEM ships stdout and
parses JSON.

NEVER the call's arguments. The caller is the verified token's subject plus
ONLY the claims an operator names in BEHEROUTER_AUDIT_CLAIMS — the one
sanctioned exception to "never log an identity value" (AGENTS.md hard
constraints, docs/IDENTITY.md). Claims come from the verified token only,
never from a request header.
"""

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import UsageError
from .logsetup import timestamp
from .outcomes import Outcome

AUDIT_VAR = "BEHEROUTER_AUDIT"
CLAIMS_VAR = "BEHEROUTER_AUDIT_CLAIMS"
logger = logging.getLogger("beherouter.audit")


@dataclass(frozen=True)
class Caller:
    auth: str  # "oidc" | "shared" | "none"
    sub: str | None = None
    claims: dict[str, str] = field(default_factory=dict)


def audit_claims() -> tuple[str, ...]:
    names = (n.strip() for n in os.environ.get(CLAIMS_VAR, "").split(","))
    return tuple(n for n in names if n and n != "sub")


def audit_enabled() -> bool:
    raw = os.environ.get(AUDIT_VAR, "").strip().lower()
    if raw in ("", "on"):
        return True
    if raw == "off":
        return False
    raise UsageError(f"${AUDIT_VAR} must be 'on' or 'off', got {raw!r}")


def caller_from_token(token, names: tuple[str, ...]) -> Caller:
    from .auth import SHARED_CLIENT_ID
    from .identity import claim_at, identity_from_token

    if token is None:
        return Caller(auth="none")
    if token.client_id == SHARED_CLIENT_ID:
        return Caller(auth="shared")
    req = identity_from_token(token)
    picked: dict[str, str] = {}
    for name in names:
        value = claim_at(req.claims, name)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            picked[name] = str(value)
    return Caller(auth="oidc", sub=req.subject, claims=picked)


def current_caller(names: tuple[str, ...]) -> Caller:
    from fastmcp.server.dependencies import get_access_token

    try:
        token = get_access_token()
    except (RuntimeError, LookupError):  # no request context (in-process use, tests)
        token = None
    return caller_from_token(token, names)


def install_handler(stream=None) -> None:
    """Called by logsetup.configure(): audit lines go to stdout, unformatted
    (each message is already a JSON object), and never into the main log."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)


class AuditSink:
    def __init__(self, enabled: bool = True, write: Callable[[str], None] | None = None) -> None:
        self.enabled = enabled
        self._write = write or logger.info

    @classmethod
    def from_env(cls) -> "AuditSink":
        return cls(enabled=audit_enabled())

    def emit(
        self, *, call_id: str, surface: str, tool: str, inner_tool: str | None,
        caller: Caller, outcome: Outcome, latency_ms: int,
    ) -> None:
        if not self.enabled:
            return
        line = {
            "ts": timestamp(time.time()),
            "event": "tool_call",
            "call_id": call_id,
            "surface": surface,
            "tool": tool,
            "inner_tool": inner_tool,
            "caller": {"sub": caller.sub, **caller.claims},
            "auth": caller.auth,
            "outcome": outcome.kind,
            "reason": outcome.reason,
            "status": outcome.status,
            "latency_ms": latency_ms,
        }
        self._write(json.dumps(line, separators=(",", ":")))

    def emit_admin(
        self, *, action: str, target_kind: str, target: str, actor: str, outcome: str
    ) -> None:
        """One line per admin request, refusals included (spec §4.3). `target`
        for a subject is its digest (admin.subject_digest), never the value; no
        body, no `reason`, no credential."""
        if not self.enabled:
            return
        line = {
            "ts": timestamp(time.time()),
            "event": "admin",
            "action": action,
            "target_kind": target_kind,
            "target": target,
            "actor": actor,
            "outcome": outcome,
        }
        self._write(json.dumps(line, separators=(",", ":")))

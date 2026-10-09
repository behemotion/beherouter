"""The ONE call path every published tool goes through.

    gate → stages → work(scope) [→ scope.execute: identity → timeout → executor]
    → classify → error result | result → audit · metrics · one log line

A tool function hands `run` a `work(scope)` coroutine. `work` may read the
catalogue (meta-tools) and reaches the backend only through
`scope.execute(verb, args)`, which resolves the caller's identity per call —
`policy.resolve()` reads FastMCP's request context, which exists only here.

Errors never leave as exceptions: FastMCP would log them as multi-line
tracebacks (outcomes.py). A non-AxiError is OUR bug, and is the one case
logged with its traceback.

`stages` is the seam later sub-projects extend (per-tool gates, rate limits,
kill switch, confirmation): each is `async def stage(scope) -> None` that
returns or raises a tagged AxiError.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import metrics
from .audit import AuditSink, Caller, current_caller
from .errors import Unavailable, tag
from .outcomes import EXECUTE, GATE, OK, PREPARE, Outcome, classify, error_result

logger = logging.getLogger("beherouter.calls")

_CALLER_TEXT = frozenset({"unknown_tool", "bad_arguments"})


@dataclass
class CallScope:
    surface: str
    tool: str
    call_id: str
    caller: Caller
    pipeline: "CallPipeline"
    phase: str = GATE
    inner_tool: str | None = None
    identity_keys: tuple[str, ...] = ()

    async def execute(self, verb: str, args: dict) -> dict:
        return await self.pipeline._execute(self, verb, args)


Stage = Callable[[CallScope], Awaitable[None]]


class CallPipeline:
    def __init__(
        self,
        surface: str,
        executor: Any,
        policy: Any | None = None,
        *,
        call_timeout_s: float | None = None,
        audit: AuditSink | None = None,
        audit_claim_names: tuple[str, ...] = (),
        stages: Sequence[Stage] = (),
    ) -> None:
        self.surface = surface
        self.executor = executor
        # None unless identity is configured AND enabled: a disabled policy
        # keeps the call path byte-identical to a gateway with no identity.
        self.active = policy if policy is not None and policy.enabled else None
        self.call_timeout_s = call_timeout_s
        self.audit = audit if audit is not None else AuditSink.from_env()
        self.audit_claim_names = audit_claim_names
        self.stages = list(stages)

    async def run(self, tool: str, work: Callable[[CallScope], Awaitable[Any]]) -> Any:
        scope = CallScope(
            surface=self.surface,
            tool=tool,
            call_id=uuid.uuid4().hex,
            caller=current_caller(self.audit_claim_names),
            pipeline=self,
        )
        start = time.monotonic()
        outcome, failure, result = OK, None, None
        try:
            if self.active is not None:
                # Gate WITHOUT materialising (identity.IdentityPolicy.guard):
                # a read-only meta-tool must not need the caller's credential.
                # It runs BEFORE any catalogue re-list, so a refused caller
                # cannot drive a re-list of a backend they may not use.
                self.active.guard()
            for stage in self.stages:
                await stage(scope)
            scope.phase = PREPARE
            result = await work(scope)
        # CancelledError is BaseException: a vanished client is not recorded.
        except Exception as e:  # noqa: BLE001 -- the boundary: classified, never raised
            failure, outcome = e, classify(e, scope.phase)
            result = error_result(e, outcome)
        self._record(scope, outcome, failure, time.monotonic() - start)
        return result

    async def _execute(self, scope: CallScope, verb: str, args: dict) -> dict:
        scope.phase = GATE
        identity = self.active.resolve() if self.active is not None else None
        if identity is not None and self.active is not None:
            # Names only. A value here would put a credential in the log.
            pending = {self.active.header: ""} if identity.pending is not None else {}
            scope.identity_keys = tuple(
                sorted({**identity.headers, **identity.env, **identity.credentials} | pending)
            )
        scope.phase = EXECUTE
        if self.call_timeout_s is None:
            return await self.executor.run(verb, args, identity=identity)
        try:
            async with asyncio.timeout(self.call_timeout_s):
                return await self.executor.run(verb, args, identity=identity)
        except TimeoutError:
            raise tag(
                Unavailable(
                    f"surface '{self.surface}': '{scope.inner_tool or scope.tool}' did "
                    f"not answer within {self.call_timeout_s:g}s"
                ),
                "timeout",
                limit_s=self.call_timeout_s,
            ) from None

    def _record(
        self, scope: CallScope, outcome: Outcome, failure: Exception | None, seconds: float
    ) -> None:
        latency_ms = round(seconds * 1000)
        metrics.observe_call(self.surface, scope.inner_tool or scope.tool, outcome.kind, seconds)
        self.audit.emit(
            call_id=scope.call_id,
            surface=self.surface,
            tool=scope.tool,
            inner_tool=scope.inner_tool,
            caller=scope.caller,
            outcome=outcome,
            latency_ms=latency_ms,
        )
        mode = self.active.mode if self.active is not None else ""
        fields = {
            "call_id": scope.call_id,
            "surface": self.surface,
            "tool": scope.tool,
            "inner_tool": scope.inner_tool,
            "outcome": outcome.kind,
            "reason": outcome.reason,
            "latency_ms": latency_ms,
            "subject": scope.caller.sub,
            "mode": mode,
            "keys": list(scope.identity_keys),
        }
        msg = (
            "call surface=%s tool=%s inner=%s outcome=%s reason=%s latency_ms=%d "
            "subject=%s mode=%s keys=%s call_id=%s"
        )
        args: tuple = (
            self.surface,
            scope.tool,
            scope.inner_tool,
            outcome.kind,
            outcome.reason,
            latency_ms,
            scope.caller.sub,
            mode,
            list(scope.identity_keys),
            scope.call_id,
        )
        extra = {"fields": fields}
        if failure is None:
            # The audit line already records an OK call; at INFO this one would
            # double the volume. With the audit off it is the only trace left.
            level = logger.debug if self.audit.enabled else logger.info
            level(msg, *args, extra=extra)
        elif outcome.kind == "internal":
            logger.error(msg, *args, exc_info=failure, extra=extra)
        elif outcome.reason in _CALLER_TEXT:
            # These messages are built from caller input (a tool name, argument
            # keys): spec §8 keeps it out of the log. Outcome and reason remain.
            level = logger.error if outcome.kind in ("unavailable", "timeout") else logger.warning
            level(msg, *args, extra=extra)
        elif outcome.kind in ("unavailable", "timeout"):
            logger.error(msg + " error=%s", *args, failure, extra=extra)
        else:
            logger.warning(msg + " error=%s", *args, failure, extra=extra)

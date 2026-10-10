"""Per-tool call gates: role gates (A7), rate limits (A10), confirm-before-write (A12).

Each gate is a pipeline TOOL STAGE: `async (scope, descriptor, args) -> None`, run inside
`CallScope.execute` once `run_tool`'s inner tool is known, before identity is materialised
and outside the call timeout. `Gates` bundles them for `build_surface`, which also asks it
what a caller may see (listing, search, describe).
Spec: docs/superpowers/specs/2026-10-08-call-gates-design.md.
"""

import asyncio
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import identity
from .errors import AuthError, AxiError, Unavailable, UsageError, tag
from .models import ToolDescriptor
from .pipeline import ToolStage

if TYPE_CHECKING:
    from .registry import RegistryEntry


@dataclass(frozen=True)
class ToolRoleGate:
    """A7: `[surface.authz.tools.<name>] require_roles`, ALL semantics, on top of the
    surface gate. Like the surface gate it carries no security weight -- the backend is
    the control -- it turns an opaque backend refusal into a sentence naming the role.

    A tool gate does NOT make the surface require a verified user: a shared-token
    caller keeps every ungated tool and is refused only the gated ones.
    """

    surface: str
    tools: Mapping[str, tuple[str, ...]]
    roles_claim: str

    def check(self, name: str) -> None:
        required = self.tools.get(name)
        if not required:
            return
        # Through the module attribute, so the live request is read the one way
        # every other gate reads it (and tests can substitute it).
        req = identity.request_identity()
        what = f"tool '{name}' on surface '{self.surface}'"
        if req.shared or not req.subject:
            # Tagged: describe_tool raises this in the PREPARE phase, where an
            # untagged AuthError would not classify as a refusal.
            raise tag(
                AuthError(
                    f"{what} requires a verified user; this caller presented the "
                    f"shared gateway token or none at all"
                ),
                "unauthenticated",
            )
        identity.check_roles(what, required, self.roles_claim, req)

    def visible(self, name: str) -> bool:
        """Fail closed: a gate that cannot be evaluated hides the tool."""
        try:
            self.check(name)
        except Exception:  # noqa: BLE001 -- a listing filters, it never raises
            return False
        return True

    async def __call__(self, scope, d: ToolDescriptor, args: dict) -> None:
        self.check(d.name)


CONFIRM_TIMEOUT_S = 300.0
_SHOWN_ARGS_MAX = 2000
_SHOWN_VALUE_MAX = 300


def _show_args(args: dict) -> str:
    """The arguments as the human sees them: every NAME and the start of every value.

    Cut per value, never as one string, so a long early value cannot push a later
    argument out of view. Anything cut is announced with the full size.
    """
    full = json.dumps(args, default=str, sort_keys=True)
    if len(full) <= _SHOWN_ARGS_MAX:
        return f"Arguments: {full}"
    cap = max(40, min(_SHOWN_VALUE_MAX, _SHOWN_ARGS_MAX // max(1, len(args)) - 40))
    lines = []
    for name in sorted(args):
        value = json.dumps(args[name], default=str)
        if len(value) > cap:
            value = f"{value[:cap]}…(+{len(value) - cap} chars)"
        lines.append(f"  {name}: {value}")
    body = "\n".join(lines)
    if len(body) > _SHOWN_ARGS_MAX:
        body = f"{body[:_SHOWN_ARGS_MAX]}…"
    shown = len(body)
    return f"Arguments (truncated, {shown} of {len(full)} characters shown):\n{body}"


@dataclass(frozen=True)
class ConfirmGate:
    """A12: a mutating call needs a HUMAN's yes, asked through MCP elicitation.

    `mutating` True or None (no readOnlyHint) needs it: unknown fails closed.
    A client that cannot elicit is refused rather than trusted -- an agent re-sending
    a flag is not a human confirming. Elicitation needs a stateful session, so a
    stateless surface can never confirm (sub-project 4 must refuse the pair).
    The arguments go to the caller's own client in the question, never to a log.
    """

    surface: str
    exempt: frozenset[str] = frozenset()
    timeout_s: float = CONFIRM_TIMEOUT_S

    def applies(self, d: ToolDescriptor) -> bool:
        return d.mutating is not False and d.name not in self.exempt

    async def __call__(self, scope, d: ToolDescriptor, args: dict) -> None:
        if not self.applies(d):
            return
        from fastmcp.server.dependencies import get_context
        from mcp import types
        from mcp.shared.exceptions import McpError

        ctx = get_context()
        wanted = types.ClientCapabilities(elicitation=types.ElicitationCapability())
        if not ctx.session.check_client_capability(wanted):
            raise self._refuse(
                d, "unsupported", "this client cannot ask its user (no MCP elicitation support)"
            )
        try:
            async with asyncio.timeout(self.timeout_s):
                # mypy picks the `response_type: None` overload for `bool`; the
                # `type[T]` one is the one that runs.
                answer = await ctx.elicit(
                    self._question(d, args),
                    response_type=bool,  # type: ignore[arg-type]
                    response_title="Confirm",
                )
        except TimeoutError:
            raise self._refuse(d, "timeout", f"no answer within {self.timeout_s:g}s") from None
        except McpError:
            raise self._refuse(d, "unsupported", "the client failed to ask its user") from None
        except ValueError:
            # FastMCP (pydantic's ValidationError is a ValueError) rejects an accept
            # with no value or a non-bool; its message quotes the answer, so drop it.
            raise self._refuse(d, "declined", "the client's answer was not a yes") from None
        if answer.action == "accept" and getattr(answer, "data", None) is True:
            return
        raise self._refuse(d, "declined", "the user did not confirm it")

    def _question(self, d: ToolDescriptor, args: dict) -> str:
        effect = (
            "changes data"
            if d.mutating
            else "may change data (its backend does not say it is read-only)"
        )
        return (
            f"Allow '{d.name}' on surface '{self.surface}'? It {effect}.\n"
            f"{_show_args(args)}"
        )

    def _refuse(self, d: ToolDescriptor, confirmation: str, why: str) -> AxiError:
        return tag(
            AuthError(
                f"'{d.name}' on surface '{self.surface}' needs a human confirmation: {why}"
            ),
            "confirmation_required",
            confirmation=confirmation,
        )


@dataclass(frozen=True)
class Gates:
    """One surface's tool stages, in their fixed order, plus what the surface asks them."""

    stages: tuple[ToolStage, ...] = ()
    roles: ToolRoleGate | None = None
    # [surface.authz] hide_tools: a caller is not SHOWN a tool their roles refuse.
    hide: bool = True
    confirm: ConfirmGate | None = None

    def visible(self, name: str) -> bool:
        """Whether the live caller is shown `name` in tools/list and search_tools."""
        return self.roles is None or not self.hide or self.roles.visible(name)

    def check_visible(self, name: str) -> None:
        """describe_tool's question: raise the refusal a hidden tool would answer with."""
        if self.roles is not None and self.hide:
            self.roles.check(name)

    def requires_confirmation(self, d: ToolDescriptor) -> bool:
        """Whether a call of `d` asks a human first (describe_tool says so up front)."""
        return self.confirm is not None and self.confirm.applies(d)


_RATE_KEYS = ("calls", "per_s", "burst")


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate_rate_limit(surface: str, raw) -> None:
    """Validate `[surface.rate_limit]` offline. No I/O."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise UsageError(f"'{surface}': rate_limit must be a table, got {raw!r}")
    unknown = sorted(set(raw) - set(_RATE_KEYS))
    if unknown:
        raise UsageError(
            f"'{surface}': unknown rate_limit key(s) {unknown}; allowed: {list(_RATE_KEYS)}"
        )
    if not _positive_int(raw.get("calls")):
        raise UsageError(
            f"'{surface}': rate_limit calls must be a positive integer, got {raw.get('calls')!r}"
        )
    per_s = raw.get("per_s")
    if (
        not isinstance(per_s, (int, float))
        or isinstance(per_s, bool)
        or not math.isfinite(per_s)
        or per_s <= 0
    ):
        raise UsageError(
            f"'{surface}': rate_limit per_s must be a positive number of seconds, got {per_s!r}"
        )
    if "burst" in raw and not _positive_int(raw["burst"]):
        raise UsageError(
            f"'{surface}': rate_limit burst must be a positive integer, got {raw['burst']!r}"
        )


# Every shared-token and anonymous caller shares one bucket: they are one identity
# as far as the gateway can tell.
SHARED_KEY = "<shared>"


@dataclass
class RateLimiter:
    """A10: a continuous-refill token bucket per caller on one surface.

    Counts calls that reach the backend only (it is a tool stage); the catalogue
    meta-tools stay free. In memory and per process: N replicas allow up to N times
    the limit, and a restart refills every bucket. Memory is bounded by sweeping
    out buckets that have refilled completely, at most once per window.
    """

    surface: str
    calls: int
    per_s: float
    burst: int
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _swept: float = float("-inf")

    @classmethod
    def from_table(cls, surface: str, raw: Mapping) -> "RateLimiter":
        calls = int(raw["calls"])
        return cls(surface, calls, float(raw["per_s"]), int(raw.get("burst", calls)))

    def matches(self, raw: Mapping) -> bool:
        """Whether `raw` (a [surface.rate_limit] table) configures this exact limit:
        a reload keeps an equal limiter, so a reload is never a free refill."""
        calls = int(raw["calls"])
        return (calls, float(raw["per_s"]), int(raw.get("burst", calls))) == (
            self.calls,
            self.per_s,
            self.burst,
        )

    @property
    def limit(self) -> str:
        """`calls/per_s` as an operator wrote it: never exponent form (`:g`
        rendered a 30-day window as 2.592e+06)."""
        per = self.per_s
        shown = str(int(per)) if per == int(per) else f"{per:f}".rstrip("0").rstrip(".")
        return f"{self.calls}/{shown}s"

    def take(self, key: str) -> float:
        """Spend one token for `key`. 0.0 when allowed, else seconds until one is."""
        now = self.clock()
        self._sweep(now)
        rate = self.calls / self.per_s
        tokens, at = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - at) * rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) / rate

    def _sweep(self, now: float) -> None:
        if now - self._swept < self.per_s:
            return
        self._swept = now
        rate = self.calls / self.per_s
        full = [
            key for key, (tokens, at) in self._buckets.items()
            if tokens + (now - at) * rate >= self.burst
        ]
        for key in full:
            del self._buckets[key]

    async def __call__(self, scope, d: ToolDescriptor, args: dict) -> None:
        caller = scope.caller
        key = caller.sub if caller.auth == "oidc" and caller.sub else SHARED_KEY
        wait = self.take(key)
        if wait:
            # Rounded first: 1/rate is a hair above a whole second in floats,
            # and ceil() of that would say "retry in 2s" for a 1 s wait.
            retry = max(1, math.ceil(round(wait, 6)))
            # Unavailable for the envelope's type and exit code; the TAG is what
            # makes the outcome `refused` / `rate_limited`, not the class.
            raise tag(
                Unavailable(
                    f"surface '{self.surface}': rate limit of {self.limit} reached for "
                    f"this caller; retry in {retry}s"
                ),
                "rate_limited",
                retry_after_s=retry,
                limit=self.limit,
            )


def gates_from_entry(
    entry: "RegistryEntry", limiters: dict | None = None
) -> Gates | None:
    """The gates one registry entry configures, in their FIXED order -- roles, rate
    limit, confirm -- or None. Validate the entry first.

    `limiters`, when given, is the gateway's `{surface: RateLimiter}`; an equal
    limit reuses the existing limiter (spec §2.5).

    The order is a contract: a caller without the role spends no token, a
    rate-limited call puts no question in front of a human, and a declined
    confirmation does spend one (or declining would be a free probe).
    """
    from .auth import roles_claim

    authz = entry.authz or {}
    stages: list[ToolStage] = []
    roles = None
    tools = identity.tool_roles(entry)
    if tools:
        roles = ToolRoleGate(entry.name, tools, roles_claim())
        stages.append(roles)
    if entry.rate_limit:
        limiter = limiters.get(entry.name) if limiters is not None else None
        if limiter is None or not limiter.matches(entry.rate_limit):
            limiter = RateLimiter.from_table(entry.name, entry.rate_limit)
        if limiters is not None:
            limiters[entry.name] = limiter
        stages.append(limiter)
    confirm = None
    if authz.get("confirm_mutating"):
        confirm = ConfirmGate(entry.name, frozenset(authz.get("confirm_exempt") or ()))
        stages.append(confirm)
    if not stages:
        return None
    return Gates(
        stages=tuple(stages),
        roles=roles,
        hide=authz.get("hide_tools", True),
        confirm=confirm,
    )


def unserved_names(entry: "RegistryEntry", served: set[str]) -> list[str]:
    """Gated or exempted tool names the backend does not serve. Offline lint has no
    catalogue, so attach is the first place this can be seen."""
    authz = entry.authz or {}
    named = set(identity.tool_roles(entry)) | set(authz.get("confirm_exempt") or ())
    return sorted(named - served)

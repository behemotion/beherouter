# Call gates: per-tool roles, rate limits, confirm-before-write

**Date:** 2026-10-08
**Status:** Approved design, not yet built
**Answers:** client asks A7, A10, A12 (`docs/requests/BEHEROUTER-WHAT-WE-STILL-NEED.md`)
**Builds on:** `2026-10-08-call-pipeline-observability-design.md` (the `CallPipeline`, the
reason vocabulary, the reserved reasons)

## 1. Goal

Three gates, each opt-in per surface, enforced by the gateway on every call that reaches a
backend, whether the call names a pinned tool or goes through `run_tool`:

| Ask | Gate | Refusal reason |
|---|---|---|
| A7 | a role list per tool, on top of the surface gate | `missing_role` / `unauthenticated` |
| A10 | a token bucket per (surface, caller) | `rate_limited` |
| A12 | a human confirmation for a mutating call, through MCP elicitation | `confirmation_required` |

With none of them configured, the call path is unchanged.

Out of scope: the surface kill switch and caller block (sub-project 3, which keeps the
reserved reasons `surface_disabled` and `caller_blocked`), quotas over longer windows, and
limits shared across replicas.

## 2. The seam: tool stages inside `execute`

`CallPipeline.stages` run before `work`, so `run_tool`'s inner tool is not yet known there.
Every call that reaches a backend goes through `CallScope.execute`, and by then the inner
tool is resolved. So per-tool gates run there:

```
run(tool, work)
  gate (policy.guard)            surface-wide: who may use this surface
  stages                         surface-wide (sub-project 3: kill switch, caller block)
  work(scope) ──► scope.execute(descriptor, args)
                    tool_stages, in order:
                      1. tool roles     A7
                      2. rate limit     A10
                      3. confirm        A12
                    identity → timeout → executor
```

- `ToolStage = Callable[[CallScope, ToolDescriptor, dict], Awaitable[None]]`: the scope, the
  resolved descriptor, and the prepared (wire) arguments, which the confirm gate shows to the
  human. A stage returns, or raises a tagged `AxiError`.
- `CallPipeline` takes `tool_stages: Sequence[ToolStage] = ()`, keyword-only.
  `build_surface` takes `gates: Gates | None = None`, keyword-only and optional, because it
  has 60+ test callers. `Gates` bundles the three policies: it supplies the pipeline's
  `tool_stages`, and answers the listing, `search_tools` and `describe_tool` questions
  (`visible`, `check_visible`, `requires_confirmation`).
- `CallScope.execute(verb, args)` becomes `execute(descriptor, args)`. Its two callers are
  `run_pinned` and `run_tool`. `_execute` runs the tool stages with `scope.phase = GATE`, so
  their errors classify as refusals, then resolves identity as today.
- **Order is part of the contract:**
  - A caller without the role spends no rate-limit token.
  - A rate-limited call never puts a dialog in front of a human.
  - A declined confirmation does spend a token. Otherwise declining would be a free way to
    probe the backend.
- Tool stages run **outside** `call_timeout_s`, which bounds only the executor. A human
  answering a confirmation is not a backend timeout.
- The meta-tools that never call `execute` (`search_tools`, `describe_tool`,
  `context_cost`) pass no tool stage. They read only the catalogue.

Rejected alternatives:
- **`run_tool` resolves its name before `stages`.** The catalogue re-list would then run
  before the stages, so a refused caller could drive a re-list. It also needs a special case
  for one tool.
- **Each tool function calls the checks itself.** The same code ends up in several places,
  and the next meta-tool can forget it.

## 3. A7: per-tool roles

```toml
[back-office.authz.tools.recategorize_call]
require_roles = ["back-office-write"]
```

- **Matching.** The key is the tool's name as the surface shows it (`ToolDescriptor.name`).
  The semantics are ALL, like the surface `require_roles`, and the check runs **in
  addition** to the surface gate.
- **The check.** It reuses `IdentityPolicy`'s role check. The check is lifted to take the
  role list as an argument, so the surface and the tool share one claim parser (a
  `$BEHEROUTER_OIDC_ROLES_CLAIM` path, a list or a space-delimited string).
  - A missing role → `missing_role` with `required_roles` and `missing_roles`.
  - A shared-token or anonymous caller → `unauthenticated`, for that tool only.
  - No roles claim configured → `UsageError` (a misconfiguration), as today.
- **The surface stays open.** A tool gate alone does **not** make the surface require a
  verified user. `IdentityPolicy.enabled` is unchanged, and a shared-token caller keeps every
  ungated tool.
- **Hiding.** Under `[surface.authz] hide_tools` (default true), a caller is not shown tools
  they cannot run:
  - `tools/list`: `_GateListing` filters out the gated pinned tools whose roles the caller
    lacks. It is installed whenever a tool gate exists, not only when the surface hides.
  - `search_tools` drops those tools from its hits.
  - `describe_tool` and `run_tool` answer `missing_role`, **not** `unknown_tool`. The role
    gate carries no security weight (the backend is the control), so naming the missing
    role is the actionable answer that A8 asked for.
  - The listing rules are unchanged: filter, never raise, and a misconfigured gate lists
    nothing.
- **Lint and attach.**
  - `registry-lint` checks the shape offline: `tools` is a table, each value is a table with
    exactly `require_roles`, and each role list is a non-empty array of non-empty strings.
  - `hide_tools` is now also valid beside `tools` alone; it no longer needs a surface-level
    `require_roles` or `audience`.
  - A tool gate counts as a role gate for the existing checks, wherever the gateway's
    environment is visible. Boot and lint refuse it when `BEHEROUTER_OIDC_ROLES_CLAIM` is
    unset, and refuse it on a `shared`-only gateway (`gates_on_caller`), exactly as for
    the surface gate.
  - There is no catalogue offline, so a gated name the backend does not serve logs a
    WARNING at attach.

## 4. A10: rate limit

```toml
[dwh.rate_limit]
calls = 60      # tokens per window
per_s = 60      # window, seconds
burst = 60      # optional bucket size; default = calls
```

- **Buckets.** One token bucket per caller on the surface. The key is `Caller.sub` for an
  OIDC caller. Every shared-token and anonymous caller shares the reserved key `<shared>`.
- **Refill.** Continuous, at `calls / per_s` tokens a second. A call spends one token.
- **Refusal.** `rate_limited` with context:
  - `retry_after_s`: an integer, rounded up;
  - `limit`: a string such as `"60/60s"`.

  The error text says when to retry.
- **Memory.** Buckets live in the process. A bucket that has refilled completely is evicted
  on the next sweep (at most once per `per_s`), so memory is bounded by the callers active
  within one window.
- **Scope (documented).** Limits are per replica and reset on restart. With N replicas, a
  caller may get up to N× the limit.
- **Clock.** `time.monotonic`, injectable for tests.
- **Lint.** `calls` and `burst` are positive integers, `per_s` a positive number, and no
  other keys are allowed. A bad value is a refusal.

## 5. A12: confirm mutating calls

```toml
[surface.authz]
confirm_mutating = true
confirm_exempt = ["list_tables"]   # optional
```

- **What needs confirmation.** A tool whose `mutating` is `True` **or `None`** (no
  `readOnlyHint` from the backend), unless it is in `confirm_exempt`. Unknown fails closed.
- **The flow:**
  1. The client lacks the MCP `elicitation` capability
     (`ctx.session.check_client_capability`) → `confirmation_required`,
     `confirmation: "unsupported"`.
  2. Otherwise the gateway sends `ctx.elicit` with a yes/no question that names the surface,
     the tool and its arguments.
  3. Accept → the stage returns.
  4. Decline or cancel → `confirmation: "declined"`.
  5. No answer within a fixed 300 s → `confirmation: "timeout"`.

  All three refusals are kind `refused`.
- **Where the arguments go.** They reach only the caller's own client, in the elicitation.
  They are never logged and never put in `context`, which follows the existing rule.
- **Telling agents in advance.** `describe_tool` adds `"requires_confirmation": true` for a
  tool that needs it. The pinned descriptions in the frozen published `tools` array are
  **not** changed.
- **Interaction with sub-project 4 (A3).** Elicitation needs a stateful session. A surface
  served stateless cannot confirm, so sub-project 4's spec must refuse `stateless` together
  with `confirm_mutating` at lint time.
- **Lint and attach.**
  - `confirm_mutating` is a boolean.
  - `confirm_exempt` is an array of strings, and only valid with `confirm_mutating`.
  - An exempt name the backend does not serve logs a WARNING at attach.
- **Clients.** Consumers without elicitation support get `confirmation_required` on every
  mutating call of such a surface. That is the intended fail-closed behaviour, and
  `client-config` output does not change. The deployment docs list which of the five
  consumers support elicitation, as far as it is known.

## 6. Vocabulary

`errors.REASONS` gains:
- `rate_limited` → `refused`
- `confirmation_required` → `refused`

`outcomes.CONTEXT_KEYS` gains `retry_after_s`, `limit` and `confirmation`. `surface_disabled`
and `caller_blocked` stay reserved for sub-project 3.

The audit line, the metrics (`outcome` label = kind) and the `beherouter.calls` line carry
the new reasons with no schema change.

## 7. Wiring

- **`src/beherouter/gates.py` (new).** `ToolRoleGate`, `RateLimiter`, `ConfirmGate`: policy
  objects built at attach from the entry, each exposing `async __call__(scope, d, args)`. They are
  bundled by `Gates` (`stages`, `visible`, `check_visible`, `requires_confirmation`), which
  `gates_from_entry(entry) -> Gates | None` builds.
- **`identity.py`.** `_AUTHZ_KEYS` gains `tools`, `confirm_mutating` and `confirm_exempt`.
  The role check is lifted to take the role list as an argument.
- **`registry.py` / lint.** Accept and validate `[surface.rate_limit]` and the new authz
  keys.
- **`gateway.py`.** `_finish_attach`, the one build site that boot and `_retry_attach`
  share, builds `Gates` and passes it to `build_surface`.
- **`surface.py`.**
  - `execute(descriptor, args)`;
  - `_GateListing` and `search_tools` filter per tool;
  - `describe_tool` adds `requires_confirmation`.

## 8. Testing

- **Unit, `tests/test_gates.py`:**
  - Roles: ALL vs a missing role; a list claim and a string claim; a shared caller →
    `unauthenticated`; no roles claim → `UsageError`.
  - Bucket: burst, refill, and the `retry_after_s` rounding, with an injected clock;
    eviction of full buckets; separate keys per subject; `<shared>`.
  - Confirm: tools with `mutating` `True`, `None` and `False`; `confirm_exempt`.
- **Through an in-process `fastmcp.Client`:**
  - Confirm with no elicitation handler → `unsupported`.
  - A handler that accepts → the call runs.
  - A handler that declines or cancels → `declined`.
  - A handler that hangs, with a patched short timeout → `timeout`.
- **Pipeline:**
  - Order: a role refusal spends no token; a rate-limited call elicits nothing; a declined
    confirmation spends a token.
  - `run_tool`'s inner tool is gated exactly like the pinned call.
  - The meta-tools are unaffected.
  - Elicitation time does not count toward `call_timeout_s`.
  - `_meta`, the audit line and the metrics carry the new reasons, and the context
    allow-list holds.
- **Listing:** a gated tool is hidden from `tools/list` and `search_tools` for a caller
  without the role, and shown to a holder; `hide_tools = false` shows it.
- **Lint:** each new config shape, valid and invalid; the roles-claim warning.
- **Gates:** ruff, mypy, and pytest with coverage ≥ 90 %.

## 9. Docs

- `docs/IDENTITY.md`: per-tool roles, hiding, and confirmation.
- `docs/DEPLOYMENT.md`: rate limits, which are per replica; the new reasons.
- `AGENTS.md`: operator signals, reason list, and the authz keys.
- README: a short mention.
- `CHANGELOG.md` § Unreleased.
- `docs/PLUGINS.md` does not change. The gates are registry-entry config, not plugin spec.

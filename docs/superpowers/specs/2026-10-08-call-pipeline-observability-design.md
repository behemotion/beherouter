# Call pipeline and observability — design

**Date:** 2026-10-08
**Status:** approved in brainstorming, awaiting spec review
**Origin:** client request `docs/requests/BEHEROUTER-WHAT-WE-STILL-NEED.md`,
items A2 (audit log), A5 (metrics), A6 (call timeout), A8 (machine-readable
refusal), A13 (logging).
**Sub-project 1 of 5.** The others, each with its own spec later:
2 call-path policy (A7 per-tool gate, A10 rate limits, A12 confirm-mutating);
3 runtime control (A4 hot reload, A11 admin endpoint / kill switch);
4 stateless sessions (A3, spike first); 5 chart `plugins.indexes[]` (A1).
Already done on `main` and not part of this work: A9 (identity mode
`exchange`), A13's variable-name and `plane_http` docstring points.

## 1. Problem

Measured against the 0.2.5 source and a production deployment's last 72 h:

- No per-call record of who called what with which outcome. Governance asks for
  one in the SIEM for ≥ 12 months; one deployment rebuilt it as backend
  middleware for a single surface.
- One metric (`beherouter_auth_rejections_total{reason}`). No call counts,
  errors, latency, session or surface-state series.
- No tool-call timeout. Only `BEHEROUTER_ATTACH_TIMEOUT_S` exists.
- A refusal is free text only. A client cannot tell "you lack the role" from any
  other failure without parsing English.
- Expected backend refusals (a Plane 400) are logged as full Rich tracebacks at
  ERROR, multi-line, which breaks log shipping; timestamps ignore `TZ`.

**Root cause of the tracebacks.** FastMCP 3.4.5 `server.py` `call_tool` logs any
exception that is not a `FastMCPError` with `logger.exception` (line ~1343). Every
`AxiError` — e.g. `backends/mcp.py` turning a backend `ToolError` into a
`UsageError` — takes that path. FastMCP middleware wraps the *outer* call, and
the logging happens in the inner call, so a FastMCP `Middleware` cannot prevent
it. The conversion must happen inside the tool function.

## 2. Architecture: the call pipeline

A new module `src/beherouter/pipeline.py` replaces `surface.build_surface`'s
inner `dispatch` closure. Every published call goes through
`CallPipeline.run(call)`: the pinned tools, `run_tool` (labelled with its inner
tool), and the four meta-tools (so a refused `search_tools` is audited too).

```
CallPipeline.run(Call{surface, tool, inner_tool?, args})
  ├─ caller = who()        # subject + operator-chosen claims, from the verified token
  ├─ start monotonic clock
  ├─ guard / identity      # today's policy.guard() + policy.resolve()
  ├─ stages                # ordered list; sub-projects 2 and 3 append here
  ├─ asyncio.timeout(call_timeout_s) → executor.run(verb, args, identity=…)
  └─ finally: outcome = classify(exc) →
        audit.emit(...) · metrics.observe(...) · log line
  returns the tool result, or ToolResult(is_error=True, text, meta=_meta)
```

Units, each testable alone:

| Unit | Module | Responsibility |
|---|---|---|
| `Call`, `Caller`, `Outcome` | `pipeline.py` | frozen dataclasses: what was called, by whom, how it ended |
| `classify(exc) -> Outcome` | `outcomes.py` | the ONE mapping from an exception to an outcome kind and `reason` |
| `CallPipeline` | `pipeline.py` | runs guard, stages, timeout, executor; always records |
| `error_result(outcome, exc)` | `outcomes.py` | builds the `ToolResult(is_error=True, …)` |
| `AuditSink` | `audit.py` | one JSON line per call on the `beherouter.audit` logger |
| `Metrics` | `metrics.py` | `prometheus_client` collectors on a private registry |
| `configure()` | `logsetup.py` | text/json handlers on root, `fastmcp`, `uvicorn` |

**A stage** is `async def stage(call: Call, caller: Caller) -> None`; it either
returns or raises an `AxiError` carrying a `reason`. This sub-project ships the
list empty apart from the existing guard; sub-projects 2 and 3 add stages
without touching the rest of the pipeline.

**Errors never leave the tool function as exceptions.** Every `AxiError` becomes
the error `ToolResult` (§3), so FastMCP's `logger.exception` path is never
reached. A non-`AxiError` (a beherouter bug) is classified `internal`, logged
once **with** its traceback, and returned as an error result whose text names
no internals beyond the exception class.

The current `identity applied` INFO line in `dispatch` is folded into the
outcome log line, which carries the same names-only fields (subject, mode,
material key names) plus the outcome.

## 3. Error results (A8)

```
CallToolResult{
  isError: true,
  content: [{type: "text", text: "<human sentence, unchanged from today>"}],
  _meta: {"io.beherouter/error": {
    type: "auth", code: 4,              # from the beheaxi envelope
    reason: "missing_role",
    context: {required_roles: ["ai-dwh-access"]}
  }}
}
```

The text stays what the model and LibreChat show today, so nothing that reads
text breaks. `reason` is a stable, documented enum:

| `reason` | Source | `context` |
|---|---|---|
| `unauthenticated` | identity resolution, no usable token | — |
| `missing_role` | `require_roles` gate | `required_roles` |
| `wrong_audience` | `[surface.authz] audience` | `expected_audience` |
| `identity_unavailable` | lookup map / exchange failure | — |
| `unknown_tool` | gateway | `suggestions` (catalogue names) |
| `bad_arguments` | gateway argument preparation | — |
| `edition_unsupported` | the McpBacking guard (Plane CE) | — |
| `backend_rejected` | backend refused the call (4xx-class) | `status` when known |
| `backend_unavailable` | backend 5xx or transport failure | `status` when known |
| `timeout` | A6 | `limit_s` |
| `internal` | anything not an `AxiError` | — |

Reserved for later sub-projects: `rate_limited`, `surface_disabled`,
`caller_blocked`, `confirmation_required`.

**Rule:** `context` never contains a caller-supplied or caller-held value — it
names what was *required*, never what the caller *had*. Raisers attach the reason
explicitly (`AxiError(context={"reason": …, …})` or a small helper); `classify`
falls back to the exception type for errors that carry none.

The `_meta` key is namespaced (`io.beherouter/error`) to match the
`io.beherouter/plugin` key used by `catalog-import`/`catalog-export`.

**To verify in the plan:** a pinned tool whose descriptor has an
`outputSchema` returns a `ToolResult(is_error=True)` without FastMCP running
output validation against it.

## 4. Logging (A13)

`beherouter.logsetup.configure()` is called by `gateway.serve()` only — the
other CLI verbs print machine output and keep Python's default logging. It
replaces the handlers on the root, `fastmcp`, `uvicorn`,
`uvicorn.error` and `uvicorn.access` loggers (FastMCP installs `RichHandler`s
of its own).

- `BEHEROUTER_LOG_FORMAT=text|json`, default `text`. `text` keeps today's
  one-line shape without Rich tracebacks. `json` writes one object per line:
  `ts` (ISO 8601 with the **local** offset, so `TZ` is honoured), `level`,
  `logger`, `msg`, plus structured fields passed via `extra`. An exception goes
  into an `exc` string field; output is never multi-line.
- Levels by outcome: `backend_rejected`, refusals, `unknown_tool`,
  `bad_arguments` → one WARNING line; `backend_unavailable`, `timeout` → one
  ERROR line, no traceback; `internal` → ERROR with the traceback.
- An unknown `BEHEROUTER_LOG_FORMAT` value is a `UsageError` at startup.

## 5. Audit (A2)

A dedicated `beherouter.audit` logger, always JSON, one line per call, written
to stdout regardless of `BEHEROUTER_LOG_FORMAT` (the SIEM ships stdout):

```json
{"ts": "2026-10-08T10:12:03.412+02:00", "event": "tool_call",
 "call_id": "6f1c…", "surface": "dwh", "tool": "run_tool", "inner_tool": "list_tables",
 "caller": {"sub": "7d2e…", "email": "a@example.com"}, "auth": "oidc",
 "outcome": "ok", "reason": null, "status": null, "latency_ms": 31012}
```

- **Arguments are never recorded.** There is no switch to record them.
- `caller.sub` comes from the verified token. `BEHEROUTER_AUDIT_CLAIMS`
  (comma-separated claim names, default empty) adds further named claims, read
  from the **verified JWT only**, never from request headers. A shared-token
  call records `{"sub": null}` with `"auth": "shared"`.
- `BEHEROUTER_AUDIT=off` disables it. Default on.
- `call_id` is a random UUID4 per call, also put on the outcome log line, so an
  audit line and an error log line join.

**Hard-constraint change.** AGENTS.md's "never log or echo an identity value"
gains one sentence: the audit line may carry the claim values an operator names
in `BEHEROUTER_AUDIT_CLAIMS`, and nothing else; no token, header, credential or
exchange material, ever. `docs/IDENTITY.md` records the same exception.

## 6. Metrics (A5)

`prometheus_client` (a new dependency), collectors on a private
`CollectorRegistry`, rendered on the existing unauthenticated `/metrics`:

| Series | Type | Labels |
|---|---|---|
| `beherouter_tool_calls_total` | counter | `surface`, `tool`, `outcome` |
| `beherouter_tool_call_duration_seconds` | histogram | `surface`, `tool` |
| `beherouter_active_sessions` | gauge | `surface` |
| `beherouter_surface_up` | gauge | `surface` (1 attached, 0 serving 503) |
| `beherouter_auth_rejections_total` | counter | `reason`, `surface` |

- `tool` is the inner tool for `run_tool`. Label values come only from the
  catalogue or the published names, never from caller input: an unknown name is
  `tool="<unknown>"`. Cardinality is bounded by catalogue size (~100/surface).
- `auth_rejections_total` keeps its name and its `reason` values, so existing
  alerts keep working; `surface` is new (`""` when the rejection happens before
  a surface is known). The old `collections.Counter` in `auth.py` is removed.
- Counters reset on restart, as all Prometheus counters do; `rate()` and
  `increase()` handle resets. The reply to the client says so.
- **Active sessions (risk).** FastMCP has no public session count. One adapter
  reads each surface's `StreamableHTTPSessionManager` state at scrape time,
  held by a test against the pinned FastMCP version. If an upgrade removes what
  it reads, the series is omitted with a WARNING at startup, never faked.

## 7. Call timeout (A6)

- Registry key `call_timeout_s` on a surface entry; falls back to
  `BEHEROUTER_CALL_TIMEOUT_S`; default **unset = no timeout** (today's
  behaviour, so an upgrade cannot cut off a slow backend).
- Applied with `asyncio.timeout` around `executor.run` only (not around guard,
  identity resolution or token exchange, which have their own bounds).
- On expiry: `reason: "timeout"`, `context.limit_s`, outcome `timeout`.
- The executor must stay usable after a cancelled call (stdio and reconnecting
  MCP executors in particular) — a test, not an assumption.
- `registry-lint` and `validate_entry` refuse a non-numeric or non-positive
  value. `plugin-config` does not emit the key.

## 8. Testing

TDD, gates per change (`ruff`, `mypy`, `pytest --cov`, floor 90 %).

- `tests/test_pipeline.py` — each outcome kind from a fake executor; exactly one
  audit line per call; `run_tool` inner-name labelling; meta-tools pass through;
  no argument value appears in audit or log output.
- `tests/test_outcomes.py` — `classify` table; `_meta` shape per reason;
  `context` never echoes caller values; an `AxiError` produces no traceback in
  the captured logs.
- `tests/test_logsetup.py` — JSON lines parse; `ts` carries an offset under a
  non-UTC `TZ`; no Rich handler remains; bad format value refused.
- `tests/test_metrics.py` — exposition parses; histogram present; `<unknown>`
  bucketing; `auth_rejections_total` name and reasons unchanged.
- `tests/test_timeout.py` — expiry, executor reuse after cancellation, lint
  refusals.
- e2e (`tests/e2e/e2e.py`) — one check that a call as alice through the Plane
  surface produces an audit line with alice's `sub` and `outcome: ok`.

## 9. Docs and release

- `docs/DEPLOYMENT.md`: logging format, audit line, metric series, timeout.
- `docs/IDENTITY.md`: the audit-claims exception.
- `AGENTS.md`: hard-constraint wording; operator signals (new metrics, audit).
- `README.md`, `CHANGELOG.md` § Unreleased, `charts/beherouter/values.yaml`
  comments for the new env vars.
- No version change in this work; the next release is a patch (0.2.6).

## 10. Out of scope

Per-tool gates, rate limits, confirm-before-write (sub-project 2); hot reload
and the admin endpoint (sub-project 3); stateless sessions (sub-project 4); the
chart's plugin indexes (sub-project 5); log shipping, SIEM retention and
scraping config (the deployer's side).

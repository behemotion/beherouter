# Reply: beherouter — what we still need (section A)

**From:** beherouter upstream
**Date:** 2026-10-09
**Replying to:** `BEHEROUTER-WHAT-WE-STILL-NEED.md` (2026-10-07), section A only.
Sections B and C are your work, and this reply makes no promises about them.

## Summary

All thirteen A items are answered in code. None of it is released yet. The next
release is **0.2.6**, a patch release with chart 0.1.7. `CHANGELOG.md` § Unreleased
is the authoritative list.

| Item | Ask | Status |
|---|---|---|
| A1 | More than one plugin index, or plugins from a ConfigMap | **Built**, ships in 0.2.6 |
| A2 | Per-call audit log | On `main`, unreleased |
| A3 | Shared or stateless MCP sessions | **Built** (the stateless option), ships in 0.2.6 |
| A4 | Hot reload of the registry | On `main`, unreleased |
| A5 | Metrics | On `main`, unreleased |
| A6 | Per-call timeout | On `main`, unreleased |
| A7 | Role gate per tool | On `main`, unreleased |
| A8 | Machine-readable refusal | On `main`, unreleased |
| A9 | Token exchange (RFC 8693) | On `main`, unreleased |
| A10 | Rate limits per user and surface | On `main`, unreleased |
| A11 | Kill switch, quarantine, admin endpoint | On `main`, unreleased |
| A12 | Confirm-before-write for every client | On `main`, unreleased (gateway side) |
| A13 | Logging, timestamps, variable names, docs | On `main`, unreleased |

"Built" means the work is done and tested but not yet merged; it is in 0.2.6.

---

## Item by item

**A1 — plugin sources.** The chart gains two values. Neither changes
`plugins.install`, `indexUrl` or `indexCredentialsSecret`.

- `plugins.indexes[]` (`name`, `url`, optional `credentialsSecret` with keys
  `username`/`password`) adds named uv indexes.
  - They are searched before `indexUrl`/PyPI.
  - uv's first-index strategy is kept, so a public package with the same name
    cannot shadow a private one.
  - Credentials reach only the init container, as
    `UV_INDEX_<NAME>_USERNAME`/`_PASSWORD` (upper-cased, `-` → `_`).
  - `name` must be `[a-z0-9-]+` and must not be `plugins`.
  - This covers back-office's GitLab project directly: option (a) from your
    list, so (b) is no longer needed.
- `plugins.local` (`configMap`, `wheels: [filenames]`) installs wheels from a
  ConfigMap. Limits: binaryData, at most 1 MiB in total, **wheels only**.
  - A plugin registers through entry-point metadata, so `dwh` ships as a wheel.
  - The wheels go through the same installer as `plugins.install`: shared
    dependencies are pinned to the gateway's versions and then pruned.
  - It removes both traps you reported:
    - **An upgrade now rolls the pods.** You list each wheel's versioned
      filename, so a new version changes the pod template.
    - **A missing ConfigMap no longer leaves the pod stuck in
      ContainerCreating.** The ConfigMap is mounted `optional`. If it or a
      listed file is missing, the init container fails with
      `plugin wheel not found: /etc/beherouter/plugins-local/<configMap>/<file>`.
  - The registry-lint hook installs the same wheels, so a missing one also stops
    `helm upgrade`.
  - Your `extraInitContainers` copy and `apply.sh` can go.
- The two can be combined with `plugins.install`. See `docs/DEPLOYMENT.md`
  § Private CA, out-of-tree plugins.

**A2 — audit.** Each tool call writes one JSON line on `beherouter.audit` (stdout).
- It carries: surface, tool, inner tool (for `run_tool`), caller `sub`, the
  claims you name in `BEHEROUTER_AUDIT_CLAIMS` (add `email` there), outcome,
  reason, status, latency and `call_id`.
- It never carries arguments.
- It is on by default; `BEHEROUTER_AUDIT=off` disables it.
- `status` is the upstream HTTP status when the backend gave one, `null`
  otherwise. An MCP backend's `isError` result carries no status, so a Plane
  400 relayed by plane-mcp-server as a tool error has `status: null` with
  reason `backend_rejected`.

**A3 — sessions.** We built the stateless option. We did not build an external
session store.
- Set `stateless = true` on a registry entry to serve that surface with no MCP
  session. A rollout or reload then loses nothing on it.
- Verified unchanged against LibreChat 0.8.7. As a control, a stateful rolling
  update fails the next call with `404 Session not found`.
- Constraints:
  - Refused together with `[surface.authz] confirm_mutating`, because
    elicitation needs a session.
  - The surface has no `beherouter_active_sessions` series.
  - GET on its endpoint answers 405, before auth.
  - FastMCP's `FASTMCP_STATELESS_HTTP` no longer has any effect.
- **Scaling out without affinity needs every surface to be stateless.** Once
  you run several replicas:
  - A reload must reach every pod. A `POST /admin/reload` through the Service
    reaches only one.
  - Rate limits are per replica.
  - The kill-switch file needs a shared volume.
- See `docs/DEPLOYMENT.md` § Stateless sessions.

**A4 — hot reload.**
- Three triggers: `kill -HUP`, `POST /admin/reload`, or
  `BEHEROUTER_REGISTRY_WATCH_S` (watches the file). Only surfaces whose entry
  changed re-attach.
- A registry that fails lint changes nothing (`/healthz` `last_reload`).
- A changed surface that fails to attach keeps serving its old app (`/healthz`
  `reload_failed`).
- Swapped-out apps drain for `BEHEROUTER_RELOAD_DRAIN_S` (default 30).
- For rotation: `${file:/abs/path}` registry values are re-read on reload.
- Chart values: `hotReload.*` and `secretFiles.*`.
- Gateway-wide environment variables still need a restart.

**A5 — metrics.** `/metrics` serves:
- `beherouter_tool_calls_total{surface,tool,outcome}` and
  `beherouter_tool_call_duration_seconds{surface,tool}` (histogram);
- `beherouter_active_sessions{surface}` and `beherouter_surface_up{surface}`;
- `beherouter_auth_rejections_total{reason,surface}`;
- from A4/A11: `beherouter_reloads_total{trigger,outcome}`,
  `beherouter_reload_last_success_timestamp_seconds`,
  `beherouter_surface_disabled{surface}` and `beherouter_blocked_subjects`.

Counters still reset on restart, so use `rate()`/`increase()`.

**A6 — timeout.** Set `call_timeout_s` on a registry entry, or
`BEHEROUTER_CALL_TIMEOUT_S` gateway-wide. Unset means no limit. When it expires,
the call ends with an MCP error result whose reason is `timeout`.

**A7 — per-tool roles.** `[surface.authz.tools.<name>] require_roles` (ALL
semantics) applies on top of the surface gate.
- It covers `run_tool`'s inner tool.
- A caller who fails it does not see the tool in `tools/list` or
  `search_tools`; `describe_tool`/`run_tool` answer `missing_role`.
- `hide_tools` is now valid with `tools` gates alone.
- This removes the need for a second write-only surface for back-office.

**A8 — machine-readable refusal.** A failed call is an error result
(`isError: true`) carrying
`_meta["io.beherouter/error"] = {type, code, reason, context}`.
- For a missing role: `reason = "missing_role"`, and `context` names the
  required roles. It never says which roles the caller had.
- The `reason` enum is documented in `docs/DEPLOYMENT.md` § Logs, audit and
  metrics.

**A9 — token exchange.** Use `[surface.identity] mode = "exchange"` (audience,
scope).
- The gateway trades the caller's verified JWT at the IdP for a token addressed
  to that backend, and forwards it as a header.
- Exchanged tokens are cached per surface and caller.
- A failed exchange never falls back to passthrough.
- The exchange's `client_secret` is a `${VAR}`.
- Declared by `mcp-http`, `openapi`, `office-mcp` and `plane-http`. Keycloak's
  per-connector clients and audiences are on your side.

**A10 — rate limits.** `[surface.rate_limit]` (`calls`, `per_s`, `burst`) is a
token bucket per (surface, caller) over calls that reach the backend.
- A refused call carries reason `rate_limited`, with `retry_after_s` and
  `limit`.
- Limits are in memory and per replica; they reset on restart and survive a
  reload of an unchanged `[rate_limit]`.

**A11 — kill switch and admin API.**
- `BEHEROUTER_KILLSWITCH_PATH` names a JSON state file that can:
  - stop every surface;
  - quarantine one surface;
  - block one caller `sub`.

  A block takes effect on the next call, so the 900 s JWT window is closed.
- Admin routes are off unless `BEHEROUTER_ADMIN_TOKEN` or
  `BEHEROUTER_ADMIN_ROLE` is set:
  - `POST /admin/reload`;
  - `GET /admin/killswitch`;
  - `PUT`/`DELETE` on `/admin/killswitch/all` and
    `/admin/killswitch/surfaces/{name}`;
  - `POST /admin/killswitch/subjects/block` and `/unblock`, with the `sub` in
    the body.
- Every admin request writes an `event: admin` audit line.
- The admin token must differ from the gateway token; boot refuses otherwise.
- Allow `/admin` at your proxy from the admin network only.
- New refusal reasons: `surface_disabled` (with context `scope`) and
  `caller_blocked`.
- Chart values: `admin.*` and `killswitch.*`.

**A12 — confirm-before-write.** `[surface.authz] confirm_mutating` (with
`confirm_exempt`) makes the gateway ask the human to confirm a mutating call, or
one whose `mutating` is unknown, through MCP elicitation, for every client.
- **It needs a client that supports elicitation and a stateful session.**
  Without elicitation, every such call is refused with reason
  `confirmation_required` and `confirmation: "unsupported"`. It cannot be
  combined with `stateless = true` (A3).
- `describe_tool` reports `requires_confirmation`.
- Your part stands: backends must set `readOnlyHint`/`destructiveHint`, or their
  tools count as unknown and need confirmation.

**A13 — quality.**
- **Logging:** `BEHEROUTER_LOG_FORMAT=json` writes one object per line. A
  backend refusal is now one WARNING line with no traceback, in either format.
- **Timestamps:** JSON logs carry a local-offset ISO timestamp.
- **Variable names:** the backend credential is
  `BEHEROUTER_<SURFACE>_API_KEY`, which is what `plugin-config` emits and the
  chart example uses. `BEHEROUTER_<SURFACE>_TOKEN` is the client bearer name.
  `registry-lint` warns on the colliding name.
- **Docs:** the `plane_http.py` docstring now matches `docs/IDENTITY.md` §6b.
  `/http` is upstream's OAuth proxy and 401s a forwarded IdP token; the IdP path
  needs `contrib/plane-mcp-bearer`.

---

## Changes visible to you after upgrading

- **Error text lost FastMCP's `Error calling tool '<name>': ` prefix.** If your
  LibreChat approval card or anything else parses error text, check it. Prefer
  `_meta["io.beherouter/error"].reason`.
- **A failed call returns `isError: true` instead of raising.**
- **An MCP backend answering HTTP 4xx** (a refused deployment or per-user
  credential, say) is now reason `backend_rejected`, with `status`. It was
  `backend_unavailable`. A 5xx stays `backend_unavailable`.
- **`/metrics`:** the Content-Type is now prometheus-client's
  (`text/plain; version=1.0.0; charset=utf-8`), values render as floats, and
  `beherouter_auth_rejections_total` gains a `surface` label (`""` when the path
  names no surface). Counters reset on restart.
- **Logs:**
  - The audit line is on by default, as JSON on stdout beside the log on stderr.
  - A successful call's `beherouter.calls` line is DEBUG while the audit is on.
  - Third-party loggers (`httpx`, `httpcore`, `mcp`) are quiet below WARNING.
- **New refusal reasons:** `rate_limited`, `confirmation_required`,
  `surface_disabled`, `caller_blocked`, `timeout`.
- **`admin`, `healthz` and `metrics` are reserved surface names.** Lint, boot
  and reload refuse them.
- **An unknown path answers an RFC 9457 `application/problem+json` 404**; it
  used to be Starlette's plain text.
- **New `/healthz` keys:** `needs_config_change`, `pinned_missing`,
  `reload_failed`, `last_reload`, `disabled`, `killswitch`. The kill switch
  never degrades `status`, so alert on `disabled`.
- **New environment variables:** `BEHEROUTER_LOG_FORMAT`, `BEHEROUTER_AUDIT`,
  `BEHEROUTER_AUDIT_CLAIMS`, `BEHEROUTER_CALL_TIMEOUT_S`,
  `BEHEROUTER_REGISTRY_WATCH_S`, `BEHEROUTER_RELOAD_DRAIN_S`,
  `BEHEROUTER_ADMIN_TOKEN`, `BEHEROUTER_ADMIN_ROLE`, `BEHEROUTER_KILLSWITCH_PATH`.
  `FASTMCP_STATELESS_HTTP` is now ignored.
- **New chart values:** `admin.*`, `killswitch.*`, `secretFiles.*`,
  `hotReload.*`, `plugins.indexes`, `plugins.local`. With `hotReload.enabled`,
  the registry checksum annotation is dropped, so editing the ConfigMap no
  longer rolls the pods.

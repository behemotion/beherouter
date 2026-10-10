# Stateless MCP sessions per surface (client ask A3) — design

**Date:** 2026-10-09
**Status:** approved (2026-10-09); built
**Sub-project:** 4 of the client A-list (`docs/requests/BEHEROUTER-WHAT-WE-STILL-NEED.md` § A3)

## Problem

Each surface serves FastMCP's stateful streamable-HTTP app: the MCP session lives in the
pod's session manager. The client runs one replica because of it, `sessionAffinity:
ClientIP` cannot spread load (all LibreChat traffic comes from one pod IP), and every
rollout fails an in-flight call with `404 Session not found`. A hot reload (sub-project 3)
ends a *changed* surface's sessions at the swap for the same reason.

## Decision

**A per-surface, opt-in stateless mode.** Not an external session store: FastMCP has no
session-store seam, a store adds a stateful dependency, and the only feature that needs a
session (`confirm_mutating`, elicitation) can stay on a stateful surface. Not a gateway-wide
switch: a `confirm_mutating` surface would then need an explicit opt-out and lint would
have to read the environment.

```toml
[plane]
plugin = "plane-http-apikey"
stateless = true
```

## Spike result (2026-10-09, throwaway, scratchpad only)

LibreChat **v0.8.7** (`ghcr.io/danny-avila/librechat:latest`, digest `sha256:2f9aee2c…3d89c9`,
`@modelcontextprotocol/sdk` 1.29.0, `mcp-protocol-version: 2025-11-25`) against FastMCP
3.4.5 mounted the way beherouter mounts it (`Mount("/spike", http_app(path="/mcp",
stateless_http=…))`), driven through real chats (an ephemeral agent with a scripted
OpenAI-compatible LLM):

| Step | Stateless | Stateful |
|---|---|---|
| `initialize` | POST, no session id → 200, none issued | POST → 200, id issued |
| `notifications/initialized` | POST → 202 | POST + id → 202 |
| GET (SSE stream) | **405**, taken silently as "no SSE" | 200, long-lived stream |
| `ping`, `tools/list`, `tools/call` | POST → 200 | POST + id → 200 |
| connection close | nothing to end | **no DELETE sent**: server sessions leak until process exit |

- LibreChat initializes **once per connection**, not per request: an inspection connection
  at boot, then one per user on first chat; later turns send only `tools/call`.
- The 405 on GET is handled explicitly by the SDK; LibreChat logs nothing for it.
- **Rollout:** stateless — the next call lands on the new process and succeeds, no
  re-initialize. Stateful rolling update — the next call carries an old id to the new
  process, gets `404 Session not found`, **the tool call fails** in front of the model, then
  LibreChat reconnects (and opened a duplicate reconnect, leaking one more session).
- Tool lists are taken only when a connection is built. `notifications/tools/list_changed`
  cannot reach a stateless client (no GET stream). Harmless here: the published `tools`
  array is frozen per app by design.
- No new client-config note: `requiresOAuth: false` and `mcpSettings.allowedDomains`
  (already emitted / documented) are all LibreChat needs.
- Not tested: multiple LibreChat users, LibreChat with Redis, idle/long-timeout behaviour.

## Design

### 1. Configuration and refusal

- `RegistryEntry.stateless: bool | None = None` (top level, like `call_timeout_s`; `None`
  and `false` mean stateful, today's behaviour).
- `validate_entry` refuses:
  - a non-bool `stateless` (`'<s>': stateless must be true or false, got …`);
  - `stateless = true` with `[<s>.authz] confirm_mutating = true`:
    `'<s>': stateless = true cannot be combined with authz confirm_mutating: confirmation
    asks the user through MCP elicitation, which needs a session`.
- `registry-lint` runs `validate_entry`, so it refuses both offline; the gateway refuses
  them at boot and a reload refuses them as a lint failure (422), like any invalid entry.
- `ConfirmGate`'s existing capability check (`gates.py`) stays as the runtime backstop.
- `stateless` is part of the entry, so it is part of `reload.fingerprint`: flipping it
  re-attaches that surface only.

### 2. Runtime

- `Supervisor._run` builds `surface.http_app(path="/mcp", stateless_http=<flag>)`. The flag
  travels with the built surface from its entry to the supervisor (boot, retry and reload
  all install through `GatewayRuntime._install`).
- **Drain is unchanged.** `_Counted` counts in-flight requests; a stateless swap has no
  sessions to end, so a reload or rollout costs a stateless client nothing.
- **The frozen published `tools` array is unchanged.** It is captured per surface at
  attach, not per session, so every request to one app sees that app's list.
- **`beherouter_active_sessions` is omitted for a stateless surface** — no series rather
  than a constant 0 that reads as "nobody connected". `_track_sessions` is skipped (an INFO
  line, not the "FastMCP does not expose its session table" WARNING); a reload that turns a
  surface stateless removes its existing gauge series.
- No `/healthz` change.

### 3. Chart

Comment-only edits, defaults unchanged, **no chart version bump** (0.1.7 is unreleased):
`values.yaml` `replicaCount`, `service.sessionAffinity` and the autoscaling note say that
ClientIP affinity is needed only for stateful surfaces, and that with every surface
stateless a rollout drops nothing and replicas can scale freely (rate limits stay per
replica; the kill-switch file still needs a shared volume).

### 4. Docs

- `docs/IDENTITY.md` (the "not built yet" note at ~416-419): the refusal is built.
- `docs/DEPLOYMENT.md`: new § Stateless sessions — when to use it, what it costs
  (`confirm_mutating`, `active_sessions`, `list_changed`), the spike's LibreChat result,
  the GET 405; cross-link from § Hot reload ("a changed surface's sessions end at the
  swap" → "unless it is stateless").
- `AGENTS.md`, `CHANGELOG.md` § Unreleased.
- `clientconfig.py`: no change.

## Testing

- `validate_entry` / `registry-lint` CLI: non-bool refused; `stateless` +
  `confirm_mutating` refused; `stateless` alone and `confirm_mutating` alone accepted.
- HTTP (ASGI, in-process): a stateless surface answers `initialize`, `tools/list` and
  `tools/call` with no `mcp-session-id` request header and none in the responses; a GET
  answers 405; a stateful surface still issues a session id.
- Metrics: no `beherouter_active_sessions` series for a stateless surface; a reload
  stateful → stateless removes the series.
- Reload: flipping `stateless` re-attaches only that surface.
- Gates: `uv run ruff check src tests && uv run mypy src && uv run pytest -q
  --cov=beherouter`; baseline 1549 passed, 93.12 %, floor 90 % never lowered.

## Out of scope

An external session store; shared rate-limit buckets across replicas; delivering
`list_changed` to stateless clients.

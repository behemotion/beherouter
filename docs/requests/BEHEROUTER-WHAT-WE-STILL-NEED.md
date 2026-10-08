# beherouter — what we still need

**Date:** 2026-10-07
**Prod:** beherouter **0.2.5** (chart 0.1.6, helm revision 14). That is upstream's latest release (2026-10-06), and upstream has **0 open GitHub issues**.
**Surfaces:** `plane` (per-user bearer), `core-agent` (claims), `dwh` (claims plus role gate `ai-dwh-access`).

This list comes from the infra repo, the handoffs in `chat`, `plane`, `pydantic-agent`, `back-office-toolset` and `mobile-business`, the AI governance documents, and the live gateway: logs from the last 72 h, `/metrics`, and the 0.2.5 source read inside the pod.
**✔ src** means I checked the item against the 0.2.5 source in the running pod (or the vendored chart). Every A item except A13's documentation notes now carries it.

Already closed, so not listed: `hide_tools`, the chart patches (upstream since 0.2.3), the crash loop on a failed attach (fixed in 0.2.5), per-user Plane, step E (the shared bearer is gone) and the LibreChat reconnect replay.

---

## Summary — top priorities

| # | Item | Owner | Why now |
|---|---|---|---|
| 1 | Attach the `back-office` surface (B1) | **infra** | Handoff open since 2026-09-28, no reply yet |
| 2 | Plugin from a second index or a ConfigMap (A1) | **upstream** (or our decision) | Blocks B1; `dwh` already uses a hand-built workaround |
| 3 | Per-call audit log with user, tool and outcome (A2) | **upstream** | Governance requires ≥12 months in the SIEM; the gateway logs no user today |
| 4 | Shared or stateless MCP sessions (A3) | **upstream** | Limits us to one replica; every rollout drops all sessions |
| 5 | Hot reload of the registry (A4) | **upstream** | Every new surface or token rotation restarts the gateway for all users |
| 6 | Real metrics, plus our scraping and alerts (A5, B5) | upstream + **infra** | Today there is one counter, it resets on restart, and nothing scrapes it |
| 7 | NetworkPolicy in front of plane-mcp, dwh-mcp and pydantic-agent (B4) | **infra** | Those backends trust `x-remote-user` only because "only the gateway reaches them" |

---

## A. Feature requests for upstream beherouter

**A1. More than one plugin index, or plugins from a ConfigMap or volume ✔ src.**
The chart's `plugins` init container renders exactly one `UV_DEFAULT_INDEX=plugins=<indexUrl>` with one credential pair (`_helpers.tpl:240-256`). Its `env:` is fixed: `extraEnv` does not reach it, so we cannot add a second uv index through values either.
`plugins.indexUrl` is a single URL, and it **replaces** PyPI. back-office-toolset publishes `beherouter-back-office==0.1.0` to its own GitLab project. Their handoff asks us to choose:
- (a) a second index, or
- (b) they publish under project 5841.

Our own `dwh` plugin already bypasses `plugins` with a hand-written `extraInitContainers` copy plus `apps/beherouter/plugins/apply.sh`. That path has two traps: a ConfigMap change rolls nothing, and a missing ConfigMap leaves the pod stuck in ContainerCreating.
- **Ask:** `plugins.indexes[]` with a credential Secret per index, **or** first-class `plugins.local` from a ConfigMap.
- **Our interim decision:** (b), which needs no upstream change.

**A2. A per-call audit log ✔ src.**
There is no audit code in the gateway. The logs hold uvicorn access lines and errors, with **no caller identity** (0 matches for an email in 72 h). Governance (07 "MCP gateway | tool, surface, user, outcome | ≥ 12 months | SIEM"; 06; G7.3) requires it. We rebuilt it ourselves for `dwh` only (the `unibank_dwh_audit.py` middleware). `plane` and `core-agent` have none.
- **Ask:** one structured JSON line per `tools/call`: timestamp, surface, tool (including the `run_tool` inner tool), caller `sub` and email, outcome (ok / tool error / refused / backend 4xx/5xx) and latency. Never arguments by default.

**A3. Shared or stateless MCP sessions ✔ src.**
Each surface mounts a stateful `http_app()`, so sessions live in the pod.
- We run **one replica** (no PDB, no HPA).
- Every rollout gives LibreChat a 404 "session lost", and an in-flight call fails (~3 s window).
- `sessionAffinity: ClientIP` cannot spread load, because all LibreChat traffic comes from one pod IP.
- Governance doc 01 calls the gateway "stateless, horizontally scalable", which is not true today.
- **Ask:** a stateless streamable-http mode, or an external session store such as Redis.

**A4. Hot reload of the registry, or per-surface attach and detach ✔ src.**
Surfaces attach only at startup; the only hot-reload is the identity map. Each of these is a full gateway restart, which then hits A3 and the LibreChat bearer replay:
- adding a surface (back-office next);
- rotating a hop token;
- changing a plugin.
- **Ask:** reload on SIGHUP or on a file change, re-attaching only the surfaces that changed.

**A5. Metrics ✔ src.**
The only metric is `beherouter_auth_rejections_total{reason}`, an in-memory `collections.Counter` that is lost on every restart. "0 reconnects" cannot be told apart from "no connections".
- **Ask:**
  - call counts, errors and latency histograms per surface and per tool;
  - an active-sessions gauge;
  - a gauge for degraded or 503 surfaces (since 0.2.5 a failed attach is *quiet*, and `/healthz` stays 200 "degraded");
  - auth rejections per surface.

**A6. Per-call timeout ✔ src.**
Only `BEHEROUTER_ATTACH_TIMEOUT_S` (30 s) exists, and there is **no tool-call timeout**. `dwh list_tables` takes ~30 s against IcebergS3. pydantic-agent asked whether there is a timeout below ~10 s, and we could not answer.
- **Ask:** `[surface] call_timeout_s`, with a clean MCP error when it is hit.

**A7. Role gate per tool, not just per surface ✔ src.**
`require_roles` is per surface (ALL semantics). back-office has write tools (`recategorize_call`, `reprocess_call`), and the connector standard says "write tools gated".
- **Ask:** `[surface.authz.tools.<name>] require_roles`, applied to `run_tool`'s inner tool as well, and also hiding the tool when `hide_tools` is on.
- **Workaround today:** a second surface holding only the write tools.

**A8. A machine-readable refusal ✔ src.**
A missing role raises `AuthError` with free text only ("you do not have access to surface 'dwh': it requires role(s) [...]"). chat asked for a stable code so it can show "you don't have DWH access". We answered "say so and we'll raise it upstream". Now that `dwh` is granted by hand, this case happens for real, because cached tool lists and agents still reach `tools/call`.
- **Ask:** a stable error code or `data.reason = "missing_role"` with `required_roles`.

**A9. Token exchange per connector (RFC 8693) ✔ src (absent).**
Today one audience (`plane-mcp`) admits a token to **every** surface, and the caller's token is passed through to Plane. Governance target pattern C: the gateway exchanges the user token at Keycloak for a token whose audience is that connector only. MCP security guidance forbids token passthrough.
- **Ask:** `identity.mode = "exchange"` (audience, scope).
- **Our side:** Keycloak per-connector clients and audiences. Meanwhile we could use 0.2.3's `[surface.authz] audience`, which we do not.

**A10. Rate limits and quotas per user and per surface ✔ src (absent).**
G1.4 requires a rate limit on every key and user, and rung 3 must be rate-capped. The `dwh` ClickHouse account has no limits of its own; the only limits are our middleware's 5000 rows / 55 s.
- **Ask:** a token bucket per (surface, user) in the registry.

**A11. Kill switch and quarantine at runtime ✔ src (absent).**
Governance doc 08 requires stopping one agent, or all of them, at the gateway, and quarantining a connector. Today that needs a registry edit and a rollout (A4). JWTs are verified statelessly, so a disabled user keeps working until their 900 s token expires.
- The gateway serves only `/healthz`, `/metrics` and one mount per surface. There is no admin route, and no code that disables a surface or blocks a caller.
- **Ask:** an authenticated admin endpoint to disable a surface or block a `sub`, with no restart.

**A12. Confirm-before-write (G6.4) for every client ✔ src (partial).**
Today only LibreChat's approval card does it, and it has to unwrap `run_tool`'s `args.action`. The agent core and REST clients bypass it.
- **Already in 0.2.5:** the gateway derives `mutating` from each backend tool's MCP `readOnlyHint`, as a tri-state (`backends/mcp.py:228`), and passes the four MCP hints through (`surface.py:74`). A tool with no annotation stays `mutating: None` (unknown), never "safe".
- **So the gap is two things:**
  1. **Backends** must annotate their tools. Ours (`dwh`) and theirs (`core-agent`, `back-office`, plane-mcp-server) should set `readOnlyHint` and `destructiveHint`, otherwise `mutating` is unknown.
  2. **Upstream:** there is no gateway-side enforcement. **Ask:** an opt-in `[surface.authz] confirm_mutating`, or a hook, so a mutating call needs an explicit confirmation from any client, not just LibreChat.

**A13. Upstream inconsistencies and quality issues.**
- **Logging:** expected backend refusals are logged as **full Rich tracebacks** at ERROR. In 72 h there were 24 tracebacks, all Plane HTTP 400s (e.g. `type_id: "task" is not a valid UUID`). This is noise for a SIEM, and multi-line logs break log shipping.
  - **Ask:** JSON log format; a backend 4xx as a single WARNING line with no traceback.
- **Log timestamps** are always UTC (`+00:00`) and ignore `TZ`.
  - **Ask:** an option, or simply JSON with ISO offsets.
- **Variable names:** `plugin-config plane` emits `${BEHEROUTER_PLANE_API_KEY}`, while the chart example uses `${BEHEROUTER_PLANE_TOKEN}`, which collides with the client-bearer name built by `clientconfig.py`.
- **Docs:** `plugins/plane_http.py`'s docstring says `/http` forwards a token. `docs/IDENTITY.md` §6b correctly says it is an OAuth proxy that 401s one.

---

## B. Our own work (infra)

**B1. Attach the `back-office` surface.**
Handoff `docs/handoffs/from-back-office-toolset/BACK-OFFICE-TOOLSET-READY-FOR-DEPLOY.md` (2026-09-28, untracked, **no reply**). To do:
1. Secrets `back-office-toolset-litellm`, `back-office-toolset-registry` and `back-office-toolset-mcp-hop`.
2. Key `BEHEROUTER_BACK_OFFICE_ACCESS_TOKEN` in `beherouter-gateway`, plus an `extraEnv` entry.
3. Decide the plugin index (A1).
4. Install `beherouter-back-office==0.1.0` and add the `[back-office]` registry entry, `claims` mode, probe `ping`.
5. Reply to them: since 0.2.5 a backend that is down during their `Recreate` rollouts is served as a 503 and retried, so it no longer crash-loops the gateway. Their `deploy/README.md` still says it does.
6. Consider a role gate for the write tools (A7).
- **Done:** product label only.

**B2. Measure `hide_tools` with a real non-holder token** on `dwh`. Never measured; this is owed to chat.

**B3. Repeat a gateway rollout while users are online.**
The 2026-10-07 06:21 rollout had nobody online, so it proved nothing about chat revision 45's `hasExpiredBearer`. Watch `beherouter_auth_rejections_total{reason="expired"}` from the LibreChat pod IP.

**B4. NetworkPolicy.**
`networkPolicy.enabled: false`. plane-mcp's verifier accepts any JWT-shaped token, and `dwh-mcp` and `pydantic-agent` trust `x-remote-user` because "only the gateway reaches them", but nothing enforces that.
- Restrict ingress to those three backends to the beherouter pod.
- Check how the Istio sidecars interact with that policy.

**B5. Scrape `/metrics`** (no ServiceMonitor or PodMonitor exists) and alert on:
- `expired` spikes;
- a degraded `/healthz`, or a surface serving 503.

**B6. Per-surface audience** (`[surface.authz] audience`). Today one `plane-mcp` audience opens all three surfaces. This is a stepping stone toward A9.

**B7. Migrate our `dwh` plugin to plugin API v1.**
It still imports `beherouter.plugins` / `beherouter.plugins.spec`. That works under 0.2.5's compatibility layer, but the layer may be removed in a later release. Pin `PluginSpec.api`.

**B8. Dev stack is behind prod.**
- `BEHEROUTER_VERSION=0.2.4`.
- The registry has only the stdio `plane` entry with a shared bearer: no `plane-http`, `core-agent` or `dwh`, and no OIDC. So role gates and `hide_tools` cannot be tested in dev.

**B9. Stale docs.**
- `apps/beherouter/README.md:3` says "a single curated `plane` surface"; there are three.
- `values.yaml`:
  - "OLD ADMIN TOKEN IS STILL ACTIVE" (revoked 2026-09-25);
  - "STILL ONE SHARED IDENTITY";
  - `gatewayTokenKey: BEHEROUTER_GATEWAY_TOKEN` (key deleted);
  - memory and startup-probe sizing for a stdio child that no longer exists.
- `CLAUDE.md` "carries NO user identity at any of its three hops".
- `apps/plane-mcp/README.md` "NOTHING POINTS AT IT YET".
- `docs/runbooks/langfuse-traces.md`, which describes the stdio chain.
- Governance connector register: lists DWH as "planned", but it has been live since 2026-10-06.

**B10. Token lifespan stopgap.**
`librechat` access tokens are 900 s (`ACCESS_TOKEN_LIFESPAN` in `bootstrap.py`). Drop it back to 300 once chat refreshes before each MCP call (C2).

**B11. Accepted risk, recorded:** GitLab (project 5841's package registry) is a runtime dependency at every pod start.

---

## C. Other teams (handoffs, not ours to do)

| | Team | Item | Status |
|---|---|---|---|
| C1 | chat | Register `core-agent` in LibreChat (`ADD-CORE-AGENT-MCP-SERVER.md`). The surface has been live at the gateway since revision 9. | Open |
| C2 | chat | Refresh the access token **before** each MCP call (revision 45 covers only the reconnect path) | Partly done |
| C3 | chat | Reconnect circuit breaker is keyed by server name only, so all `plane` users share one budget | Parked until B3 |
| C4 | chat | Report from a `dwh` non-holder in the UI (together with B2) | Open |
| C5 | back-office-toolset, pydantic-agent | Plugin CI tests against `beherouter@0dadad92` (0.2.2), but prod is 0.2.5 (plugin API v1, `fastmcp<4`). Their docstring still says LibreChat sends no per-user JWT. | Open |
| C6 | plane-mcp / plane | Agents send type **names** where UUIDs are expected (`type_id: "task"`), which caused 9 of the 12 recent backend 400s. The wrapper could resolve names, like it now splits comma-joined ids. | New, ours (plane-mcp wrapper) |

`mobile-business` (APK portal) has no beherouter ask.

---

### Evidence pointers
- `apps/beherouter/README.md`, `apps/beherouter/values.yaml`, `apps/beherouter/plugins/dwh/`
- `docs/handoffs/from-back-office-toolset/BACK-OFFICE-TOOLSET-READY-FOR-DEPLOY.md`
- `docs/handoffs/from-chat/PLANE-SURFACE-ROLE-MISSING-FOR-USERS.md`, `LIBRECHAT-DWH-ROUTER-GAP-CLOSED.md`, `LIBRECHAT-MCP-RECONNECT-REPLAYS-EXPIRED-BEARER-REPLY.md`
- `../chat/docs/handoffs/from-infra/LIBRECHAT-DWH-MCP-SERVER-REPLY-2.md`, `-REPLY-3.md`
- `../pydantic-agent/docs/handoffs/from-infra/CORE-AGENT-OUTPUT-RETRY-LIMIT-REPLY-2.md`
- `docs/ai-governance/` 01, 05, 06, 07, 08
- Source checked in the pod: `/app/src/beherouter/{gateway,identity,auth}.py` (0.2.5)

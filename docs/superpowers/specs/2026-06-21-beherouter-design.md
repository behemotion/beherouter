# beherouter — Layer-2 Design Spec

> **Retroactively renamed.** This document was written when the layer was called
> `behemcp`. Every occurrence was rewritten to `beherouter` on 2026-08-02 by
> `docs/superpowers/specs/2026-08-02-beherouter-rename-design.md`, filename
> included. Nothing else about it was changed — read the names as `behemcp`
> wherever the date predates 2026-08-02.

**Date:** 2026-06-21 · **Status:** Approved (design), pending spec-review + plan
**Layer:** Connectivity (foundation §4) · **Depends on:** beheaxi v0.1.0
**Conforms to:** `2026-06-21-harness-foundation-design.md` (Layer-0, source of truth)
**Supersedes framing in:** `docs/DESIGN.md` (Approach B / FastMCP / BM25 — still the reasoning record)

---

## 1. Purpose & scope

beherouter is the harness **connectivity hub**: one always-on FastMCP gateway that fronts multiple
backends and re-exposes each as its own MCP **surface** (`/<tool>/mcp`) to agents/chatbots, without
bloating the client context window. Context control = a few **pinned** flat tools per surface **+**
embeddings-free **BM25** `search_tools` / `describe_tool` / `run_tool` meta-tools for the long tail.

### 1.1 Bi-modal backends (the central decision)

The foundation frames integration as "beherouter invokes a tool's CLI" (harness tools); DESIGN.md frames
it as "mount existing MCP servers" (Plane, Gitea). These are **two backend kinds**, not a contradiction.
beherouter supports both:

| Kind | Backend | `attach` does | Runtime execution |
|---|---|---|---|
| **`cli`** | a beheaxi-CLI harness tool | subprocess `<tool> describe --json` → validate against `manifest_schema.json` → synthesize surface | **deferred this iteration** (see §1.2) |
| **`mcp`** | an existing MCP server (stdio/http) | connect, list tools, mount as a proxy | **live** — FastMCP forwards calls |

### 1.2 What this iteration delivers

Both kinds, both proven with **real** backends (no stubs for the proof path):

- **`mcp` kind — proven with Gitea** (stdio, ~100 tools): mounted, listed, and searchable. Fully
  functional including tool execution. This is the clean BM25-search proof per the handoff.
- **`cli` kind — proven with beheaxi** (the real, shipping CLI): `attach beheaxi` runs
  `beheaxi describe --json`, validates, and synthesizes a surface (pinned flat tools + BM25 search).
  **Surface synthesis, pinned-listing, and search are proven.** Live verb **execution is a deferred
  seam**: `CLIExecutor` is a defined interface whose sole implementation raises `Unavailable`
  (exit 6) with a detail pointing at the behelib milestone. Rationale: a `cli` backend that is its
  own Podman service can't be executed against until that service is deployed (first real case:
  behelib). Deferring execution — not the whole `cli` path — keeps the gateway honest (clear
  "not yet wired" envelope, never a silent failure) while still proving describe→surface today.
  **Explicitly:** this iteration a `cli` pinned flat tool is *listable and searchable but not
  callable* — invoking it (or `run_tool` against a `cli` backend) returns `Unavailable` (exit 6).
  No test or demo should assert `cli`-backend tool callability this cycle.

**Deliverables:**

1. FastMCP gateway server — port **47100**, no DB, state in `registry.toml`.
2. `registry.toml` + `beherouter attach <tool>` / `beherouter detach <tool>`.
3. beherouter's own CLI on **beheaxi** (dogfood): verbs `surfaces, attach, detach, health, search,
   describe`; pin `surfaces / health / search` (foundation §5).
4. Shared-token gateway auth + per-user backend credential passthrough.
5. Gitea surface deployed to **dev-4** per `deploy.md`.
6. `beheaxi conformance "beherouter"` green.

**Out of scope this iteration (designed, sequenced later):**
- Plane backend mounting (located, registry-ready — §5.3; sequenced after the Gitea proof).
- Live `cli` verb execution wiring (→ behelib build).
- LibreChat/Hermes/Mac consumer wiring beyond what the Gitea proof needs.

## 2. Components

Small, single-purpose modules (each independently testable):

| Module | Responsibility | Depends on |
|---|---|---|
| `registry.py` | load/save `registry.toml`; one entry per attached tool | stdlib `tomllib` + a writer |
| `naming.py` | flatten space-joined verb paths → flat MCP tool names; **collision detection** | — |
| `backends/cli.py` | run `describe --json`, validate vs `manifest_schema.json`, synthesize surface; `CLIExecutor` interface (deferred impl) | `naming`, beheaxi schema |
| `backends/mcp.py` | connect + mount an MCP backend (stdio/http) as a FastMCP proxy | `fastmcp` |
| `surface.py` | assemble one surface: pinned flat tools **+** `search_tools`/`describe_tool`/`run_tool` (FastMCP `BM25SearchTransform`) over the full verb/tool set | `fastmcp` |
| `auth.py` | gateway static-token verifier (`$BEHEROUTER_GATEWAY_TOKEN`); per-session header passthrough to backends | `fastmcp` |
| `gateway.py` | FastMCP app: assemble all surfaces from registry; listen on 47100 | all of the above |
| `cli/` | `BeheaxiApp` exposing the six verbs; dogfoods beheaxi | `beheaxi`, `registry`, `gateway` |

### 2.1 `registry.toml` shape

```toml
[gitea]
kind = "mcp"
transport = "stdio"
cmd = "gitea-mcp"           # argv; environment supplies GITEA_HOST/token per-session

[beheaxi]
kind = "cli"
cmd = "beheaxi"             # console script invoked for `describe --json` (and, later, verbs)

# later (located, not mounted this iteration):
# [plane]
# kind = "mcp"
# transport = "http"
# url = "https://<plane-mcp-slim-endpoint>/mcp"
```

Schema validation of the registry itself: `kind ∈ {cli, mcp}`; `cli` requires `cmd`; `mcp` requires
`transport ∈ {stdio, http}` and (`cmd` for stdio | `url` for http). Unknown keys → `UsageError`.

## 3. Data flow

**attach `<tool>`:**
1. Read the registry entry (else `NotFound`, exit 3).
2. `cli`: subprocess `<cmd> describe --json` → JSON-parse → validate vs `manifest_schema.json`
   (else `Unavailable`, exit 6 — describe failed/invalid) → flatten verb names + collision-check
   (§4; collision → `Conflict`, exit 5) → register a surface: pinned verbs become flat MCP tools;
   **all** verbs go into a BM25 index backing `search_tools`/`describe_tool`/`run_tool`.
3. `mcp`: connect the backend (stdio spawn / http connect; unreachable → `Unavailable`) → list tools
   → mount a proxy at `/<tool>/mcp` → BM25-index the tool list for search; pinned set from registry
   (optional `pinned = [...]` override) or a sensible default.

**client → gateway (runtime):**
1. Bearer-token check (`auth.py`); missing/wrong → reject.
2. Route to `/<tool>/mcp`. Client calls a pinned flat tool **or** a meta-tool.
3. `mcp` backend `run_tool`/flat tool → proxy forwards the call **with per-session user creds**
   (headers passed through, e.g. Gitea PAT; Plane two-header later).
4. `cli` backend `run_tool`/flat tool → `CLIExecutor.run(...)` → **deferred**: `Unavailable`
   (exit 6) with detail naming the behelib milestone.

**State:** `registry.toml` only — no database (foundation §6: beherouter has no DB).

## 4. Naming & collision rule (resolves beheaxi spec §11)

**beheaxi v0.1.0 reality (verified against `beheaxi/src/beheaxi/app.py`):** verbs register as **flat**
Typer commands; the default verb name is `fn.__name__.replace("_", "-")` — i.e. flat, **hyphenated**
strings (`read-multi`, `shelf-create`), single words otherwise. There are **no nested command groups
yet** (beheaxi's own spec §11/foundation §3.1 anticipate them as a future form). beherouter's flatten
rule is built to be correct today *and* forward-compatible: **normalize both hyphens and spaces to
underscores**, then join with `<tool>`:

```
tool "behemem", verb "read-multi"   -> behemem_read_multi   (v0.1.0 hyphenated form)
tool "behelib", verb "shelf-create" -> behelib_shelf_create
tool "behelib", verb "shelf create" -> behelib_shelf_create (future nested-group form, same result)
```

At **attach** time, build the complete set of flattened names. If two **distinct** verbs map to the
same flat name (e.g. a hyphenated `read-multi` and an explicitly-named `read_multi`), **fail the
attach** with `Conflict` (exit 5), naming both colliding verbs. Fail-fast beats silent shadowing; the
tool author fixes the verb name (the foundation can later forbid underscores in verb names at the
beheaxi layer).

## 5. Auth & credentials

### 5.1 Gateway auth — shared token
The gateway requires a static bearer token to be reachable at all. Token read from
`$BEHEROUTER_GATEWAY_TOKEN` (**never** committed). FastMCP auth verifier rejects missing/incorrect
tokens. Every consumer (LibreChat, Hermes, Mac clients, coding agents) configures this token once.

### 5.2 Backend credentials — per-session passthrough
Per-user backend credentials are forwarded per-session via FastMCP arbitrary-header passthrough
(Gitea PAT now; Plane PAT + `x-workspace-slug` two-header later). The gateway holds no per-user
backend secrets. Keep the LibreChat **anonymous-probe → 200 + placeholder `Authorization`** behavior
on surfaces that need it (so LibreChat doesn't misread a 401 as "OAuth required"). List each surface
host in the consumer's `mcpSettings.allowedDomains`.

### 5.3 Plane backend (located; mounted later)
`plane-mcp-slim.py` is at `$HOMELAB_REPO/babylon/web/plane-mcp-slim.py`, deployed on the web VM
(.95) as a FastMCP **HTTP** server (`uvx --from plane-mcp-server==0.2.8`). It already carries the
`ALLOW` trim, `SanitizePlaneArgsMiddleware` (400-fixer), `LenientHeaderAuth` (anonymous-probe trick),
and the 3-step presigned MinIO attachment upload. beherouter mounts it **as-is** as an `mcp`/`http`
backend — never reimplemented. Its anonymous-probe and two-header passthrough are subsumed by §5.2.

## 6. Error handling

beherouter's CLI uses beheaxi's problem+json envelope and the canonical `ExitCode`:

| Condition | Exit |
|---|---|
| ok | 0 |
| internal error | 1 |
| bad args / unknown registry key shape | 2 (`UsageError`) |
| tool/cmd not in registry / not found | 3 (`NotFound`) |
| (gateway auth failure surfaced via CLI) | 4 (`AuthError`) |
| verb-name collision at attach | 5 (`Conflict`) |
| backend unreachable / `describe` failed or schema-invalid / deferred `cli` exec | 6 (`Unavailable`) |

The gateway returns MCP-protocol errors to clients; the CLI maps backend failures to the codes above.

## 7. Testing

- **Unit:** registry round-trip + validation · `naming` flatten + collision detection ·
  describe→surface synthesis (from a sample manifest) · BM25 search returns expected verbs ·
  token verify accept/reject.
- **Integration:** `attach beheaxi` (real `describe --json`) → `surfaces` lists it, `search` finds a
  known verb · `attach gitea` (stdio spawn) → mount + list + BM25 search proof · gateway rejects a
  bad/missing token · `detach` removes the surface.
- **Conformance:** `beheaxi conformance "beherouter"` passes (dogfood: describe schema, pinned verbs,
  json-parseable, no-color, usage→exit-2, dashboard).
- **Deferred:** live `cli` verb execution test — added when behelib deploys.

## 8. Deployment (per `deploy.md`, foundation §6)

dev-4.example.com, rootless Podman + `podman-compose` + systemd user unit (`beherouter-compose.service`,
`Type=oneshot` + `RemainAfterExit=true`) + `loginctl enable-linger`, Caddy vhost
`beherouter.example.com` → `localhost:47100`. The beherouter container bundles `gitea-mcp` (stdio
backend) and `beheaxi` (cli-attach proof + the dogfood CLI). `$BEHEROUTER_GATEWAY_TOKEN` and backend
env supplied out-of-band. **Verify:** `curl -fsS https://beherouter.example.com/...` healthy; unit
active; linger on; a client reaches a pinned Gitea tool and the long tail via `search_tools`.

## 9. Dependencies

- `beheaxi @ git+https://github.com/behemotion/beheaxi@v0.1.0` (versioned git dep) **+** a local-path
  `[tool.uv.sources]` override for co-development against `~/Repo/BEHEMOTION/beheaxi`.
- `fastmcp>=2.0.0` (BM25 search transform, mounting, header passthrough, auth), `httpx`, `uvicorn`.
- Public beheaxi API consumed: `BeheaxiApp, Status, AxiError, ExitCode, NotFound, AuthError,
  Conflict, Unavailable, UsageError`. Validate attached manifests against
  `beheaxi/src/beheaxi/manifest_schema.json`.

## 10. Open items / deferred

- Live `cli` verb execution (`CLIExecutor` real impl) — and the runtime-exec model (subprocess to a
  co-located binary vs `podman exec` into the tool's service container) — decided when behelib lands.
- Plane backend mounting + the full LibreChat/Hermes/Mac consumer wiring.
- Whether to expose Plane's non-`ALLOW` tools via `search_tools` against the upstream server (future
  refinement; plane-mcp-slim's trimmed tools are currently unreachable).
- **User-owned (not design):** credential rotation (Plane keys, dev-4 password, GitHub/OAuth/Gitea/
  memory-MCP tokens). Plaintext keys/password also live in `$HOMELAB_REPO/PLANE.md` (homelab repo).

# BEHEMOTION Harness — Layer-0 Foundation Design

**Date:** 2026-06-21 · **Status:** Approved (design), pending spec-review + per-tool specs
**Scope:** The cross-cutting contract every capability layer conforms to. Each tool (beheaxi,
beherouter, behelib, behemem, behetask, behedaemon) gets its own Layer-2 spec → plan → implementation
cycle conforming to *this* document.

---

## 1. Vision & layer model

A modular agentic harness: six sibling repos under `~/Repo/BEHEMOTION/`, each owning one capability
layer, attachable/detachable, sharing one MCP hub, one CLI standard, one AXI framework, one DB stack,
and one deployment convention.

| Repo | Layer | Role | Disposition |
|---|---|---|---|
| **beheaxi** *(new)* | AXI framework | Shared Python CLI/AXI framework lib + standard + conformance tests | **Build first** |
| **beherouter** | Connectivity | FastMCP gateway: tool registry, attach/detach, pinned-verbs + BM25 search | Scrap homelab/Plane-Gitea-only framing; build gateway |
| **behelib** | Knowledge | Agentic + graph RAG over docs/sources | Preserve crawl/index/search engine, evolve to graph+agentic RAG; strip docbro/bablib |
| **behemem** | Memory | Durable, cited recall | Preserve memory engine + eval; port Node→Python; own server in-repo; shed OpenCode-platform cruft |
| **behetask** | Task-tracking | Shared store of *what work exists & who owns it* | Greenfield (build from existing spec) |
| **behedaemon** | Operational | Runs tasks on the user's behalf (cron/scheduled jobs) + a library of unstructured **playbooks** for recurring dev/planning situations | **Scrap finance entirely**, greenfield |
| **beheskills** | Skills | Per-tool CLI-connectivity skills + existing `handoff`/`act` | Add one skill per tool |

## 2. Architectural principles

1. **The CLI is the one contract.** Each tool repo owns its engine + an AXI-compliant CLI — the
   single interface. The MCP surface (beherouter) and the AXI ergonomics (beheaxi) are *wrappers* over
   that CLI, never re-implementations of the verbs. beherouter reaches a tool by invoking its CLI
   (subprocess), so the integration boundary is language-agnostic.
2. **One stack: Python + uv.** All tools are Python ≥3.12, managed with `uv`, built on the **beheaxi**
   framework. behemem's Node memory server is ported to Python.
3. **Preserve proven engines, scrap divergence.** Reuse working cores (behelib RAG pipeline, behemem
   memory engine + eval harness) and refit them onto the standard; scrap divergent/dead code entirely
   (behedaemon's finance platform; all old-name configs).
4. **Attach/detach modularity.** A tool joins the harness by being registered with beherouter (and listed
   in the shared deploy registry); it leaves by being de-registered. No tool hard-depends on another at
   runtime; they coordinate through beherouter (MCP) and `/handoff` (docs).

## 3. beheaxi — the shared CLI/AXI framework

beheaxi is a Python library every tool's CLI is built on, so the AXI standard is enforced *by
construction* rather than re-implemented per tool. It provides:

- **A CLI framework** (Typer/Click-based) with shared command wiring.
- **Standard global flags:** `--json` (machine output), `--quiet`, `--no-color`.
- **No-arg invocation** → a live, actionable dashboard for the tool (current state + suggested next
  commands), per AXI principles.
- **Token-efficient default output**; filtered/targeted queries over full dumps; combined operations
  where natural.
- **Structured error envelope** (problem+json-style) and **categorized exit codes** (`0` ok; distinct
  non-zero classes for usage/not-found/auth/conflict/internal).
- **`<tool> describe --json`** — the registration contract (see §4). Generated from the command tree,
  so it can never drift from the actual CLI.
- **A conformance test suite** every tool runs in CI; "AXI-compliant" is verified, not aspirational.

### 3.1 `describe --json` schema

```json
{
  "tool": "behelib",
  "version": "1.0.0",
  "summary": "Knowledge layer — agentic + graph RAG over local docs.",
  "verbs": [
    {
      "name": "search",
      "summary": "Ranked semantic+graph search over indexed knowledge.",
      "args": [{"name": "query", "type": "string", "required": true},
               {"name": "--shelf", "type": "string", "required": false}],
      "pinned": true,
      "mutating": false
    }
  ]
}
```

`pinned` marks the common verbs beherouter exposes as flat MCP tools; the rest are reachable via search.
`mutating` lets beherouter/agents distinguish read vs write operations.

## 4. beherouter — registration & attach/detach

> This CLI-derived `describe --json` contract **supersedes** the earlier "config schema + backend
> behe-surface spec" framing in `AUDIT-2026-06-21.md` — generating the manifest from the command tree
> is drift-proof (it can't disagree with the actual CLI).

- beherouter keeps a **`registry.toml`**: `[behelib] cmd = "behelib"`, one entry per attached tool.
- **`beherouter attach <tool>`** → runs `<tool> describe --json`, generates a FastMCP surface mounted at
  `/<tool>/mcp`: **pinned verbs → flat MCP tools**; everything else reachable via
  **`search_tools` / `describe_tool` / `run_tool`** (embeddings-free **BM25** — the design already
  chosen in `docs/DESIGN.md`). **`beherouter detach <tool>`** removes the surface.
- beherouter's **own** CLI (`surfaces` / `add` / `remove` / `health` / `search` / `describe`) is built on
  beheaxi — the hub dogfoods the standard.
- Surfaces are per-consumer: Claude Code, OpenCode, LibreChat, etc. each mount any subset.
- Credentials/headers are forwarded per-session (FastMCP arbitrary-header passthrough).

## 5. Per-tool verb contracts (settled in audit; refined in each Layer-2 spec)

- **behemem:** `write, read, patch, search, list, read-multi` (+ `status`); pin `write/read/search/status`.
- **behelib:** `search` (core), `fill`, `shelf`, `box`, `health`, `serve`; pin `search/health`.
- **behetask:** `list, get, create, update, delete` + checklist/context ops; pin `list/get/create/update`.
- **behedaemon:** `schedule, run, status, logs, jobs` + `playbook add/list/run`; pin `run/status/jobs`.
- **beherouter:** `surfaces, attach, detach, health, search, describe`; pin `surfaces/health/search`.

## 6. Deployment & stack standard

**Single dev/deploy target: `dev-4.example.com` (203.0.113.100 — confirmed healthy 2026-06-21:
Podman 5.8.2, Caddy active, linger on).** This supersedes the old homelab web VM (203.0.113.95)
still referenced in `beherouter/AGENTS.md`/`docs/DESIGN.md` history — those mentions are now stale.

- **Access:** password auth only (no committed key):
  `sshpass -p "$DEV4_SSH_PASS" ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no aleksandr@dev-4.example.com`.
  `DEV4_SSH_PASS` is supplied out-of-band — **never** hardcoded in a committed file.
- **Runtime:** rootless **Podman** + `podman-compose`, each repo self-contained: a `podman-compose.yml`
  + a **systemd user unit** (`<tool>-compose.service`, `Type=oneshot` + `RemainAfterExit=true`) +
  `loginctl enable-linger aleksandr` — survives reboots.
- **DB:** **PostgreSQL (latest)** for every stateful tool. Each tool's compose ships its own Postgres
  container (self-contained / attach-detachable); each connects to its own DB.
- **TLS / routing:** system **Caddy** (`/etc/caddy/Caddyfile`), wildcard certs at
  `/opt/certs/server/`. Each service gets a vhost `<tool>.example.com` → `reverse_proxy
  localhost:<app-port>`. *Note:* `babylon/setup-dev-caddy.sh`'s `DEV_IPS` map lacks dev-4 — either add
  `[dev-4]="203.0.113.100"` or write the Caddyfile vhost directly.
- **Unconventional ports.** Canonical registry (every repo's `deploy.md` embeds this for mutual
  awareness):

| Service | App port | DB port | URL |
|---|---|---|---|
| beherouter (gateway / MCP front door) | 47100 | — (config-file state) | https://beherouter.example.com |
| behelib | 47110 | 47119 | https://behelib.example.com |
| behemem | 47120 | 47129 | https://behemem.example.com |
| behetask | 47130 | 47139 | https://behetask.example.com |
| behedaemon | 47140 | 47149 | https://behedaemon.example.com |

- **`deploy.md` in every repo** follows one shared template: the dev-4 environment + SSH/Podman/
  systemd/linger/Caddy convention, this repo's port(s)+URL, its Postgres usage, and the **full sibling
  registry** above (so each repo is aware of all others).

## 7. Cross-cutting standards

- **/handoff:** bootstrap a root `HANDOFF.md` (registry of all siblings) in every repo; distribute the
  skill via `beheskills/handoff/scripts/sync.sh`. Per-tool **skills** authored after each CLI lands.
- **Secrets:** purge all plaintext secrets from tracked/config files; reference by env var; user rotates
  the live values (root passwords, Plane PAT/keys, OAuth/GitHub tokens). History scrub out of scope
  (rotation invalidates leaked values).
- **Identity / old configs:** complete each in-tree rename as part of that tool's rework (avoids a risky
  big-bang). Stale **config files** (gitea/homelab/automcp/automem/family-daemon/behenedger, dead
  `.mcp.json`) are ditched immediately.

## 8. Build sequence

```
P0  secret purge + stale-config ditch + coherent deploy.md   (parallel, now)
1.  beheaxi      — framework + describe schema + conformance   (everything depends on it)
2.  beherouter      — registry + gateway consuming `describe`     (the hub)
3.  behelib      — proving-ground tool (preserve+evolve)
4.  behemem      — port Node→Python, own the server
5.  behetask     — greenfield build
6.  behedaemon   — scrap finance, greenfield (cron + playbooks)
7.  /handoff bootstrap + per-tool beheskills skills rollout
```

## 9. Deferred to per-tool Layer-2 specs

- behelib: the graph + agentic RAG design (graph store, retrieval strategy, what of the qdrant/sqlite-vec
  pipeline is kept vs replaced).
- behemem: storage-era decision (markdown/ripgrep vs Postgres+pgvector) under the Postgres-standard
  constraint; server ownership.
- behetask: build-out of the existing FastAPI+SQLite spec, migrated to Postgres.
- behedaemon: scheduler design (cron engine), playbook-library format, execution sandbox.
- beherouter: gateway implementation details; LibreChat/consumer wiring.
- Exact in-tree rename mechanics per tool.

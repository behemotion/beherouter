# beherouter

**A multi-service MCP gateway that gives an agent a small, curated tool surface over many
backends — using lexical search, not embeddings.**

[![CI](https://github.com/behemotion/beherouter/actions/workflows/ci.yml/badge.svg)](https://github.com/behemotion/beherouter/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## The problem

An MCP client pays context for **every tool definition a backend advertises**, on every
request, before the model has done anything. Connect a handful of real backends and a
client is carrying hundreds of tool schemas it will never call.

beherouter fronts each backend with a short **pinned** tool list plus a lexical **search**
tier that finds the rest on demand. Its `plane` surface advertises 11 tools instead of 30;
an agent pays 14 definitions rather than 30 and can still reach all of them.

Search is **BM25 plus fuzzy matching — never embeddings.** At the scale that matters here
(~100 tools per surface) lexical ranking is accurate enough, and it means no model to host,
no index to rebuild, and no vector store to operate.

## Two faces

**To agents** — one endpoint per backend at `/<surface>/mcp`, serving a few pinned verbs
plus `search_tools`, `describe_tool`, `run_tool` and `context_cost`.

**To backends** — every backend is a versioned, tested **plugin** carrying its own pins,
probe, config schema, credential names and quirk workarounds. Attaching one is a name in
`registry.toml`, not an act of archaeology.

Built on [FastMCP](https://gofastmcp.com). The gateway is an MCP *server* to clients and an
MCP *client* to backends.

## Quickstart

```bash
git clone https://github.com/behemotion/beherouter && cd beherouter
uv sync --extra dev

uv run beherouter plugins          # what can be attached, attaching nothing

touch registry.toml                # start empty — no surfaces yet
export BEHEROUTER_GATEWAY_TOKEN=$(openssl rand -hex 32)
BEHEROUTER_REGISTRY=registry.toml uv run beherouter serve
```

From another shell:

```bash
curl -s localhost:47100/healthz
# {"status":"ok","surfaces":[]}
```

An **empty surface list is a valid state** — that response means the gateway is
installed and serving.

### Attaching a backend

A surface is a plugin name plus overrides:

```toml
[office]
plugin = "office-mcp"

[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "acme"
  [plane.env]
  api_key = "${PLANE_TOKEN}"
```

Validate it **before** starting the gateway — offline, no network, no attach:

```bash
uv run beherouter registry-lint --path registry.toml
# {'ok': True, 'path': 'registry.toml', 'surfaces': ['office', 'plane']}
```

⚠️ **The backend must actually be reachable, and its credentials must exist.** An
attach failure crash-loops the whole gateway, taking every other surface and
`/healthz` with it; an unset `${VAR}` raises at startup and does the same. That is
why `registry-lint` exists, and why `health --deep` makes a *real credentialed call*
per backend rather than just listing catalogues — a revoked token lists and searches
perfectly and fails only on a real call.

## Operator commands

| Command | Does |
|---|---|
| `plugins` | What can be attached, with backing and summary |
| `plugin-config <surface> <plugin>` | Emits the registry block, reverse-proxy clause and env line **from one spec**, so they cannot disagree |
| `registry-lint [--path P]` | Validates a registry offline — no network, no attach |
| `context-cost [--surface S]` | What each surface's published tools cost a client's context |
| `surfaces` | Attached surfaces and the plugin behind each |
| `health [--deep]` | `--deep` makes a real credentialed call per backend |
| `search` | BM25-search a backend's tools |
| `client-config <agent>` | Paste-ready MCP config for a client. Never emits a credential, only a placeholder |
| `attach` / `detach` | Add or remove a surface |
| `serve` | Run the gateway |

Every command speaks JSON (`--json` where applicable) and follows the
[beheaxi](https://github.com/behemotion/beheaxi) CLI contract: reserved exit codes, RFC
9457-shaped errors, machine-readable `describe`.

## Plugins

A plugin is frozen, inert data (`PluginSpec`: pins, probe, config schema, credential names,
backing) plus one `async build(ctx) -> Backend`. Keeping the declaration inert is
load-bearing: it lets `plugins`, `plugin-config` and `registry-lint` read a plugin **without
attaching anything**, and it makes "attach performs no network I/O" mechanically enforceable
— a plugin *cannot* phone home from its spec, only from `build`.

Four backings:

| Backing | What it is |
|---|---|
| `http` | An MCP server reached over HTTP |
| `stdio` | An MCP server run as a subprocess — costs a process, not a container |
| `cli` | A beheaxi CLI, described and invoked as tools |
| `native` | In-process Python — no sidecar, no extra runtime |

Writing one: **[`docs/PLUGINS.md`](docs/PLUGINS.md)**.

## Deployment

One container, one port (47100), config-file-only state (`registry.toml`), no database. The
gateway binds loopback and expects a reverse proxy to enforce per-client tokens.

Artifacts:

- Image: **`ghcr.io/behemotion/beherouter`** — published on every release tag
  (multi-arch amd64/arm64). Self-build from the repo-root
  [`Containerfile`](Containerfile); [`podman-compose.yml`](podman-compose.yml)
  runs it on a single host
- Kubernetes: **[`charts/beherouter`](charts/beherouter)** (Helm) — defaults to
  the published image, so the only required value is the gateway token; the
  `registry-lint` pre-deploy gate runs as a pre-install/pre-upgrade hook Job

Full guide, including the pre-deploy `registry-lint` gate and the failure modes worth
knowing before you hit them: **[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)**.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/PLUGINS.md`](docs/PLUGINS.md) | Writing a plugin: the contract, the four backings, pins and probes, testing |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Deploying, verifying, upgrading, rolling back |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Why build rather than adopt — the aggregator survey and the LiteLLM-MCP spike |
| [`docs/FASTMCP-NOTES.md`](docs/FASTMCP-NOTES.md) | FastMCP 3.x API notes |
| [`AGENTS.md`](AGENTS.md) | Working context for coding agents — the live operational detail |

## Attribution

beherouter is licensed under the Apache License 2.0 — see [`LICENSE`](LICENSE).

Apache 2.0 §4(d) requires the attribution notices in [`NOTICE`](NOTICE) to be reproduced in
any redistribution or derivative work, including wherever your product displays third-party
notices.

We additionally **request** — this is a request, not a licence term — that products built on
beherouter credit **Behemotion — https://behemotion.com** and **Aleksandr Mezin** in their
user-facing credits or terms of service.

## Licence

Apache License 2.0. Copyright 2026 Behemotion.

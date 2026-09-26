# beherouter

**A multi-service MCP gateway that gives agents a small, curated tool surface over many
backends — and makes every call act as the user who asked for it.**

[![CI](https://github.com/behemotion/beherouter/actions/workflows/ci.yml/badge.svg)](https://github.com/behemotion/beherouter/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## The problem

Connecting agents to real systems through MCP runs into two problems at once. The
first one costs you money. The second one costs you an audit trail.

**1. Every tool definition costs context.** An MCP client pays for **every tool a
backend advertises**, on every request, before the model has done anything. Connect a
handful of real backends and each client carries hundreds of tool schemas it will
never call.

**2. Every call is made as the same account.** The usual way to put an MCP server in
front of a team is one service credential: a PAT, an API key, an OAuth grant, minted
once and pasted into the server's config. From then on, every user's agent acts as
that one account. The ticket Alice's assistant closed and the meeting Bob's assistant
booked are both attributed to `mcp-bot`. The backend's own permission model doesn't
apply, because the backend never learns who is calling. And there's nothing to audit
except "the bot did it". That's tolerable in a homelab. In a company it rules the
setup out.

The two problems multiply. Each extra backend adds more context cost and another
shared credential, so the more useful the agent gets, the worse both become.

beherouter handles both in one hop:

- **Context:** each backend is fronted by a short **pinned** tool list plus a lexical
  **search** tier (`search_tools` / `describe_tool` / `run_tool`) that finds the rest on
  demand. The `plane` surface advertises 11 tools instead of 30: an agent pays for 14
  definitions and can still reach all 30.
- **Identity:** the gateway **verifies the caller** (an OIDC JWT from your IdP, accepted
  alongside or instead of a shared token) and **forwards that caller's identity** to each
  backend in the form that backend expects: their own token, asserted headers, their own
  PAT, or a per-user secret looked up for them. Two people on one gateway act as
  themselves in the same backend. This is verified end to end against a real
  `plane-mcp-server` in [`tests/e2e/`](tests/e2e/README.md).

Search is **BM25 plus fuzzy matching, never embeddings.** At the scale that matters
here (~100 tools per surface), lexical ranking is accurate enough, and it means no model
to host, no index to rebuild and no vector store to run.

## Two faces

**To agents**: one endpoint per backend at `/<surface>/mcp`, serving a few pinned verbs
plus `search_tools`, `describe_tool`, `run_tool` and `context_cost`. The published tool
list is frozen at attach, so a catalogue refresh never invalidates a host's prompt cache.

**To backends**: every backend is a versioned, tested **plugin** that carries its own
pins, probe, config schema, credential names, identity modes and quirk workarounds.
Attaching one is a name in `registry.toml`, not an act of archaeology.

Built on [FastMCP](https://gofastmcp.com). The gateway is an MCP *server* to clients and
an MCP *client* to backends.

## Identity passthrough for corporate use

> **Default behaviour is unchanged.** With no `BEHEROUTER_AUTH_MODE` and no
> `[surface.identity]`, the gateway works exactly as a shared-token gateway: one
> client token, one backend credential. Identity is opt-in, surface by surface.

Identity has two halves, configured in two different places.

### Identity in — who is calling (gateway-wide)

| `BEHEROUTER_AUTH_MODE` | Accepts |
|---|---|
| `shared` (default) | The deployment's gateway token |
| `oidc` | Only a JWT from your IdP, checked against its JWKS, `iss` and `aud` |
| `both` | Both at once, so clients that can't mint a JWT keep working next to those that can. This is the migration path |

```bash
BEHEROUTER_AUTH_MODE=both
BEHEROUTER_OIDC_ISSUER=https://sso.example.com/realms/corp
BEHEROUTER_OIDC_AUDIENCE=plane-mcp,wiki-mcp        # any of these
BEHEROUTER_OIDC_JWKS_URI=https://sso.example.com/realms/corp/protocol/openid-connect/certs
BEHEROUTER_OIDC_ROLES_CLAIM=realm_access.roles     # Keycloak; `roles` on Entra, `groups` elsewhere
```

The JWKS URI is set explicitly and never discovered at boot, so a slow IdP can't take
`/healthz` down. Keycloak, Entra ID, Okta, Authentik or any other OIDC issuer that
publishes a JWKS will work.

### Identity out — who the backend sees (per surface)

Each surface picks **one** mode, or none. There's deliberately no "optional" setting: a
surface either requires a verified user or doesn't mention identity. That rules out the
state this whole feature exists to prevent, where a surface is attached, believed to be
per-user, and actually shared.

| Mode | Use it when the backend… | What gets forwarded |
|---|---|---|
| `bearer` | verifies the **same** tokens your gateway does | the caller's own JWT, unchanged. Nothing is stored |
| `claims` | trusts the gateway to say who is calling (`REMOTE_USER`-style) | claims mapped onto headers (`x-remote-user = email`). A missing claim refuses the call rather than sending a partial identity |
| `client` | takes a per-user PAT the client can send | named client headers, **allow-listed**. Everything else the client sent is dropped, so there's no header smuggling |
| `lookup` | needs a real secret the user can't send per request (an OAuth refresh token, a minted PAT) | the caller's entry from a mounted identity map, keyed by a claim. Hot-reloaded, so rotating a credential needs no restart |

The mode's output lands wherever the backing needs it: HTTP headers for `http`, a
subprocess environment for `cli`, a per-user credential provider for `native`. A
`stdio` backing is **refused**, because a long-lived subprocess can never carry a
per-request identity. Before this rule, a per-user config on stdio attached green and
forwarded nothing. Now it's refused at lint, at boot and at transport build.

Four surfaces can run four different modes on one gateway:

```toml
[plane]                                 # each caller's own Plane PAT
plugin = "plane-http-apikey"
  [plane.config]
  base_url = "http://plane-mcp:8211/http/api-key/mcp"
  workspace_slug = "acme"
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_API_KEY}"   # attach + catalogue only
  [plane.identity]
  mode = "client"
    [plane.identity.map]
    authorization = "x-plane-pat"
  [plane.authz]
  require_roles = ["ai-plane-access"]
  audience = "plane-mcp"

[gcal]                                  # each caller's own Google calendar
plugin = "gcal"
  [gcal.identity]
  mode = "lookup"
  key = "email"
  path = "/etc/beherouter/identity-map.toml"
    [gcal.identity.map]
    refresh_token = "refresh_token"
```

**Plugins that support per-user identity today:**

| Plugin | Modes | Notes |
|---|---|---|
| `office-mcp` | `bearer`, `claims`, `client` | |
| `plane-http-apikey` | `client` | each caller's Plane PAT. Works against stock `plane-mcp-server` |
| `plane-http` | `bearer` | each caller's **IdP token**, through [`contrib/plane-mcp-bearer`](contrib/plane-mcp-bearer/README.md). Plane itself must verify it |
| `gcal`, `m365` | `lookup` | each caller's OAuth refresh token, from the identity map |
| `openapi` | `bearer`, `claims`, `client`, `lookup` | each caller's material, on the upstream REST request |

A plugin that declares no identity support **can't** be configured for per-user use.
Your own plugins declare their modes the same way (see [`docs/PLUGINS.md`](docs/PLUGINS.md)).

### Access control at the edge

`[surface.authz]` narrows who may use a surface:

- **`require_roles`**: every listed role must be present in the caller's roles claim.
- **`audience`**: the token's `aud` must name this surface. This stops one team's
  tokens from being accepted by another team's surfaces on a shared gateway.

Both gates apply to the search meta-tools as well as to calls, so a surface that refuses
your calls also refuses to list itself for you. Both are checked before the backend is
contacted, so an excluded caller can't drive traffic to it. They turn an opaque backend
`401` into a sentence that names the surface. **They are ergonomics, not the control:**
the backend's own verification of the forwarded identity is what enforces access.

### Operating it

- **Offline validation.** `registry-lint` rejects every misconfiguration it can see
  before deploy: a mode the plugin doesn't support, identity on `stdio`, an empty map, a
  missing identity map, a gate on a shared-only gateway, and more.
- **Prove a user, not just the deployment.** A green `health --deep` proves the
  deployment credential, and its output says so (`probe_scope:
  "deployment-credential"`). To prove the per-user path, probe *as a user*:

  ```bash
  beherouter health --deep --surface plane --bearer-file - --json < token.txt
  # "user_probe": {"state": "ok", "subject": "alice",
  #                "backend_identity": {"email": "alice@corp.example"},
  #                "matches_caller": true}
  ```

  `mismatch` catches exactly the incident this feature exists to prevent: every user's
  call quietly acting as the deployment identity. The token is read from a file or
  stdin, never from argv.
- **Audit logs without secrets.** Each applied identity is logged with the subject, the
  mode and the *names* of the material applied, never a value.
- **Expired tokens are called out.** An expired token is logged at WARNING and answered
  with RFC 6750 `error_description="token expired"`, so a client can tell "refresh"
  from "re-authenticate".
- **Metrics.** `GET /metrics` (counters only, unauthenticated) exposes
  `beherouter_auth_rejections_total{reason}` for `expired | invalid | issuer | audience | scope`.
- **Kubernetes.** The chart's `auth.*`, `identityMap.*` and `caBundle.*` values feed
  both the Deployment and the `registry-lint` hook Job, so the pre-deploy gate sees the
  environment that will actually serve. `caBundle` covers an IdP or backend behind a
  private CA. Without it, every JWT fails while the shared-token path stays green.

### What it doesn't do

Plan around these limits:

- **The catalogue isn't per-user.** Attaching a backend, listing its searchable
  catalogue and running `health --deep` all use the deployment credential. Only the
  *calls* are per-user. A tool a user can't use in their own backend may still be listed.
- **The published tool list isn't private.** The frozen `tools` array is served to
  anyone the gateway authenticates, even a caller the gates would refuse.
- **It isn't your authorization layer.** The gates only catch refusals early. The
  backend decides.

Full guide, with every refusal rule and worked examples: **[`docs/IDENTITY.md`](docs/IDENTITY.md)**.

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

An **empty surface list is a valid state**: that response means the gateway is
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
  api_key = "${BEHEROUTER_PLANE_API_KEY}"
```

Validate it **before** starting the gateway. This runs offline, with no network and no
attach:

```bash
uv run beherouter registry-lint --path registry.toml
# {'ok': True, 'path': 'registry.toml', 'surfaces': ['office', 'plane']}
```

⚠️ **The backend must actually be reachable, and its credentials must exist.** A
surface whose attach fails, or takes longer than `BEHEROUTER_ATTACH_TIMEOUT_S` (default
30 s), answers `503` while the gateway retries it, and `/healthz` reports
`"status": "degraded"`. An unset `${VAR}` is worse: it refuses boot, so the whole
gateway stays down. That's why `registry-lint` exists, and why `health --deep` makes a *real credentialed call* per
backend instead of only listing catalogues: a revoked token still lists and searches
fine and fails only on a real call.

To make a surface per-user, add `[surface.identity]` (see
[Identity passthrough](#identity-passthrough-for-corporate-use) above).

## Operator commands

| Command | Does |
|---|---|
| `plugins` | What can be attached, with backing and summary |
| `plugin-config <surface> <plugin>` | Emits the registry block, reverse-proxy clause and env line **from one spec**, so they can't disagree |
| `registry-lint [--path P]` | Validates a registry offline, including every identity and authz rule. No network, no attach |
| `context-cost [--surface S]` | What each surface's published tools cost a client's context |
| `surfaces` | Attached surfaces and the plugin behind each |
| `health [--deep] [--bearer-file F]` | `--deep` makes a real credentialed call per backend. `--bearer-file` repeats it as a user and reports who the backend saw |
| `search` | BM25-search a backend's tools |
| `client-config <agent>` | Paste-ready MCP config for a client. Never emits a credential, only a placeholder |
| `attach` / `detach` | Add or remove a surface |
| `serve` | Run the gateway |

Every command speaks JSON (`--json` where applicable) and follows the
[beheaxi](https://github.com/behemotion/beheaxi) CLI contract: reserved exit codes, RFC
9457-shaped errors, machine-readable `describe`.

## Plugins

A plugin is frozen, inert data (`PluginSpec`: pins, probe, config schema, credential
names, identity support, search vocabulary, backing) plus one
`async build(ctx) -> Backend`. Keeping the declaration inert is load-bearing. It lets
`plugins`, `plugin-config` and `registry-lint` read a plugin **without attaching
anything**, and it makes "attach performs no network I/O" mechanically enforceable: a
plugin *can't* phone home from its spec, only from `build`.

Five backings:

| Backing | What it is | Per-user identity |
|---|---|---|
| `http` | An MCP server reached over HTTP | per-call headers |
| `stdio` | An MCP server run as a subprocess. Costs a process, not a container | **never** (refused) |
| `cli` | A beheaxi CLI, described and invoked as tools | subprocess environment |
| `native` | In-process Python. No sidecar, no extra runtime | per-user credential provider |
| `inproc` | An in-process FastMCP server: the `openapi` source and decorator plugins | per-call headers on the upstream request |

Plugins don't have to live in this tree. A package that advertises the
`beherouter.plugins` entry point is picked up at startup, so a team with an internal
backend extends the gateway instead of forking it. On Kubernetes, the chart installs
such packages with `plugins.install`.

Writing one: **[`docs/PLUGINS.md`](docs/PLUGINS.md)**.

## Deployment

One container, one port (47100), config-file-only state (`registry.toml`, plus an
optional read-only identity map), no database. The gateway binds loopback and expects a
reverse proxy in front of it.

Artifacts:

- Image: **`ghcr.io/behemotion/beherouter`**, published on every release tag
  (multi-arch amd64/arm64, runs as UID 1000). Build it yourself from the repo-root
  [`Containerfile`](Containerfile). [`podman-compose.yml`](podman-compose.yml) runs it
  on a single host
- Kubernetes: **`oci://ghcr.io/behemotion/charts/beherouter`** (Helm; source in
  [`charts/beherouter`](charts/beherouter)). It defaults to the published image, so the
  only required value is the gateway token (none at all in pure `auth.mode=oidc`). The
  `registry-lint` pre-deploy gate runs as a pre-install/pre-upgrade hook Job

Full guide, including the pre-deploy `registry-lint` gate and the failure modes worth
knowing before you hit them: **[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)**.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/IDENTITY.md`](docs/IDENTITY.md) | Per-user identity: OIDC next to the shared token, the four modes, the identity map, role and audience gates, user probes, every refusal rule |
| [`docs/PLUGINS.md`](docs/PLUGINS.md) | Writing a plugin: the contract, the five backings, pins and probes, identity support, out-of-tree plugins, testing |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Deploying, verifying, upgrading, rolling back |
| [`tests/e2e/`](tests/e2e/README.md) | The local end-to-end stack that proves per-user identity against a real `plane-mcp-server` |
| [`contrib/plane-mcp-bearer`](contrib/plane-mcp-bearer/README.md) | The Plane mount that forwards a caller's IdP token |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Why build rather than adopt: the aggregator survey and the LiteLLM-MCP spike |
| [`docs/FASTMCP-NOTES.md`](docs/FASTMCP-NOTES.md) | FastMCP 3.x API notes |
| [`AGENTS.md`](AGENTS.md) | Working context for coding agents: the live operational detail |

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

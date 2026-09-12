# beherouter — post-deployment cleanup design

> **Retroactively renamed.** This document was written when the layer was called
> `behemcp`. Every occurrence was rewritten to `beherouter` on 2026-08-02 by
> `docs/superpowers/specs/2026-08-02-beherouter-rename-design.md`, filename
> included. Nothing else about it was changed — read the names as `behemcp`
> wherever the date predates 2026-08-02.

**Date:** 2026-07-30 · **Status:** Approved (design), implementing
**Depends on:** `2026-06-21-beherouter-design.md` (the build spec — unchanged by this)
**Touches:** this repo, `../beheaxi`, `$HOMELAB_REPO` (deployment)

---

## 1. Why

beherouter went live on 2026-07-29 at `https://beherouter.example.com` (service VM, 203.0.113.114,
deployed from the homelab repo). A 2026-07-30 audit of the running service found the
infrastructure correct and the product non-functional:

| Probe | Result |
|---|---|
| `/healthz`, unauthenticated | `200 {"status":"ok","surfaces":["gitea-home"]}` |
| `/` and `/gitea-home/mcp`, unauthenticated | `401` (Caddy default-deny) |
| `tools/list` with a per-client token | 9 tools — 6 pinned + search/describe/run, over a 53-tool backend |
| `search_tools("issue comment")` | `["issue_read","issue_write","timetracking_read"]` |
| `tools/call get_me` | ❌ `invalid username, password or token` |

The gateway, its auth, the Caddy per-client split and BM25 search all work in production. The
only attached surface's Gitea PAT is dead, so **every** call that reaches the backend fails.

The decision taken: **remove `gitea-home` entirely** rather than rotate the PAT — real services
get attached later, deliberately. That turns four audit items into this spec.

### 1.1 The diagnostic lesson (drives §4)

A revoked credential was invisible to every health signal we had. `tools/list`,
`search_tools` and `describe_tool` are answered from the **attach-time catalogue** cached in the
gateway; only `tools/call` reaches the backend's credential. So any check built on listing
verifies the plumbing and asserts nothing about authorization — which is how a dead PAT stayed
green for a day, past both `/healthz` and the Prometheus blackbox probe.

---

## 2. Scope

1. Let an empty registry serve (enables §3 without downing the service).
2. Remove `gitea-home` from the deployment.
3. Probe-based deep health check.
4. Release `beheaxi` v0.1.1; move this repo's pin off v0.1.0.
5. Reconcile the status docs across both repos.

Out of scope: attaching any new backend, Plane, live `cli`-kind execution, LibreChat/Hermes
wiring. All still deferred by the build spec §10.

---

## 3. Empty-registry serving

`gateway.py:110-114` raises `UsageError` when the registry is empty. With `gitea-home` gone that
would crash-loop the container and take `/healthz` — and its Prometheus probe — down with it,
turning an intentional emptying into an outage alarm.

**Change:** drop the refusal. Zero surfaces builds a Starlette app carrying only the `/healthz`
route, answering `{"status":"ok","surfaces":[]}`; `_combined_lifespan([])` is an empty
`AsyncExitStack` and needs no change.

**Deliberately unchanged:** `SharedTokenVerifier.from_env(strict=True)` still refuses to boot
without `$BEHEROUTER_GATEWAY_TOKEN`. An *unauthenticated* gateway is a security failure and must
stay a boot failure; an *empty* one is a valid operating state. These are different things and
the code should say so.

Caddy needs no compensating change: its `@denied` matcher is default-deny, so an empty gateway is
reachable only at `/healthz` no matter what a client asks for.

---

## 4. Deep health — `beherouter health --deep`

### 4.1 The probe key

Which call is cheap *and* authenticating is backend-specific (`get_me` for gitea-mcp), so it is
configuration, not code. `RegistryEntry` gains an optional `probe: str | None` — the flat name of
one backend tool to call with no arguments. `load_registry` already rejects unknown keys against
the dataclass fields, so the new key is validated by construction.

### 4.2 Semantics

`beherouter health --deep` attaches each registry entry fresh — a black-box check independent of the
running gateway process — and calls its probe where one is configured. Per-backend record:

```json
{"name": "gitea-home", "attach": "ok", "probe": "ok", "error": null}
```

| Field | Values |
|---|---|
| `attach` | `ok` \| `failed` |
| `probe` | `ok` \| `failed` \| `none` (no probe configured) \| `unsupported` (`cli` kind) |
| `error` | the failure message, or absent |

`probe: "none"` reports as **unknown, never green** — that honesty is the entire point of the
feature. `cli`-kind entries report `unsupported` because execution is still deferred.

Exit: `0` when nothing failed; `Unavailable` (beheaxi's code) when any attach or probe failed, so
a monitoring wrapper can use the exit status alone. Plain `beherouter health` keeps its current
registry-count behavior, unchanged. An empty registry deep-checks to `ok` with zero records.

### 4.3 Why CLI-only

No new HTTP route. `/healthz` stays shallow — a fan-out would make the probe as slow and as flaky
as the slowest backend and turn one backend's outage into a gateway alarm — and stays the only
unauthenticated route. Adding an authenticated `/healthz/deep` would need a matching clause in
Caddy's default-deny matcher, and that matcher is the client-isolation boundary: the single most
expensive thing in this system to get subtly wrong. The check runs instead as
`podman exec beherouter beherouter health --deep --json`, which a node_exporter textfile collector can
consume later without touching the auth boundary at all.

---

## 5. Removing `gitea-home`

All four edits are in `$HOMELAB_REPO` (the deployment source of truth); none are code.

| File | Change |
|---|---|
| `ur/service/beherouter/registry.toml` | `[gitea-home]` commented out beside the existing client templates, with why (dead PAT, retired 2026-07-30) |
| `ur/service/Caddyfile` | drop the `/gitea-home/*` + token clause; `@denied` reduces to `not path /healthz` |
| `ansible/playbooks/templates/beherouter-env.j2` | drop `BEHEROUTER_GITEA_HOME_TOKEN` |
| `secrets/<credential-ledger>` | annotate the Gitea PAT **and** the per-surface client token as retired |

The Prometheus blackbox probe **stays**: the gateway stays live, so the probe stays meaningful.

The ansible vault is **not** touched. The gitea-home vault key becomes unreferenced,
which is inert; decrypting and re-encrypting the vault to delete a value that is already dead is
risk without benefit.

---

## 6. beheaxi v0.1.1

`../beheaxi` carries an uncommitted fix: `BeheaxiApp` caught only one of the two importable
`ClickException` classes, so in an environment where both standalone `click` and Typer's vendored
copy are present (FastMCP causes exactly this), usage errors exit 1 instead of 2 and the
`usage_exit_2` conformance check fails. Commit it with its test, verify beheaxi's own suite and
self-conformance, tag **v0.1.1**, push branch and tag. Then bump this repo's pin from
`beheaxi @ git+…@v0.1.0` to `@v0.1.1` and relock.

### 6.1 Vendoring stays — amended from "un-vendor"

`$HOMELAB_REPO/ur/service/beherouter/vendor/beheaxi` is a copy, and the audit filed it as drift
risk. Un-vendoring would make the host build fetch a **private** GitHub repo, so the ansible role
would need a token build-secret — which is precisely what vendoring was chosen to avoid.

The real complaint was narrower: the copy carried an **uncommitted** fix, so nothing upstream
described what was deployed. Once v0.1.1 is tagged and the pin moves, the vendored tree is a
pinned mirror of a released tag and is diff-verifiable against it. Keep vendoring, re-vendor from
the tag, and state in `HARNESS-DIVERGENCES.md` that it is a deliberate token-free-build choice
with a verification command — not an open gap.

---

## 7. Docs to reconcile

`AGENTS.md` (this repo) is the worst offender: it still says *"built, not deployed (2026-07-28)"*
and *"Task 14 (deploy to dev-4) is not done: it needs `$DEV4_SSH_PASS`"*. It is the first file an
agent loads, so a fresh session would start by re-deploying a running service to the wrong host.

Reconcile to one story — live on the service VM, deployed from homelab, zero surfaces attached,
beheaxi v0.1.1 — across: this repo's `AGENTS.md`, `README.md`, `HARNESS-DIVERGENCES.md` (#1
closed, #2/#4/#5 rewritten) and `deploy.md`; plus homelab's `ur/service/AGENTS.md` § beherouter and
the stale vendored `README.md`.

---

## 8. Testing

TDD for both code changes; the existing 98 tests stay green.

- empty registry → `build_gateway_app` returns an app whose `/healthz` answers `surfaces: []`;
  `serve` no longer raises; missing token still refuses.
- `probe` accepted by `load_registry`/`save_registry` round-trip; an unknown sibling key still
  rejected.
- `health --deep`: probe ok, probe failed, no probe, `cli`-kind unsupported, attach failure, empty
  registry; exit code `Unavailable` on any failure.

Repo-level gate: full suite **with `.venv/bin` on `PATH`** (four integration tests shell out to
`beheaxi`; without it they fail as `Unavailable`, which misreads as a product bug) plus
`beheaxi conformance "beherouter"` 6/6.

Deployment gate: `ansible-playbook playbooks/service.yml --tags beherouter` **and**
`ansible-playbook playbooks/caddy.yml -l service` — two playbooks, because the first does not
ship the Caddyfile. Then re-probe `/healthz` for `surfaces: []` and confirm a request carrying the
retired client token now 401s. This step touches live infra and is taken only on explicit
go-ahead.

### 8.1 Outcome (2026-07-30)

Deployed and verified. `ansible-playbook … --tags beherouter` → `changed=6, failed=0`;
`playbooks/caddy.yml -l service` → `changed=2, failed=0`.

| Check | Result |
|---|---|
| `/healthz` unauthenticated | `200 {"status":"ok","surfaces":[]}` |
| retired client token @ `/gitea-home/mcp`, with and without trailing slash | `401` |
| retired client token @ `/` | `401` |
| in-container `beherouter health --deep --json` | `{"ok": true, "count": 0, "backends": []}`, exit 0 |
| in-container `beheaxi conformance "beherouter"` | 6/6, including `usage_exit_2` |
| in-container `beherouter <bad-verb>` | exit 2 |
| sibling vhosts (office-mcp, nocodb, service) | unaffected |

Two things the run itself taught, both now documented rather than remembered:

1. **The Caddyfile is a separate playbook.** After only `--tags beherouter`, the gateway had dropped
   the surface but the edge still honored its token, so the retired token passed Caddy and
   collected 404s from the gateway. Harmless that day; a live hazard the moment another surface
   claims that path.
2. **`--check --diff` earns its keep on vendored trees.** The dry run showed the vendor sync about
   to ship 5.3 MB of `vendor/beheaxi/.mypy_cache` to the host and into the image — the rsync
   excluded `__pycache__` but not tool caches. Fixed in `service.yml`'s `rsync_opts`.

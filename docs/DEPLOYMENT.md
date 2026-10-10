# Deploying beherouter

## What you are deploying

One container, one port, no database.

| | |
|---|---|
| **Process** | `beherouter serve --host 0.0.0.0 --port 47100` |
| **State** | `registry.toml` only — config, not data |
| **Persistence** | none. No volume, no database, no writable state |
| **Network posture** | bind loopback; a reverse proxy is the per-client token boundary |

Having no state is what makes rollback cheap, and it is worth preserving.

## The image

Published at **`ghcr.io/behemotion/beherouter`** — multi-arch (`linux/amd64`,
`linux/arm64`), one tag per release: git tag `v0.2.0` → image tag `0.2.0`
(semver, no leading `v`). `latest` follows the newest release.

```bash
docker pull ghcr.io/behemotion/beherouter:0.2.0
```

The release workflow builds the image from the repo-root
[`Containerfile`](../Containerfile) on every `v*` tag, after the full test
suite passes on the tagged revision, and refuses a tag that disagrees with the
`pyproject.toml` version or the chart's `appVersion` — the tag, the image and
the chart default cannot drift apart.

The image runs as **UID 1000** (`USER` in the Containerfile), so it is
non-root without any `securityContext`. It also ships **`plane-mcp-server`
0.3.2** in a venv of its own at `/opt/plane-mcp`, which is where the in-tree
`plane` plugin's default `cmd` points. Build with
`--build-arg PLANE_MCP_VERSION=` to leave it out.

Build it yourself only if you need to: a registry mirror, or an extended image
(any other stdio backend's binary must be **inside** the image). The
Containerfile builds from a plain checkout of that tag.

⚠️ **Podman with a writable bind mount:** the repo's `podman-compose.yml` sets
`userns_mode: keep-id:uid=1000,gid=1000`, so a host directory you own stays
writable to UID 1000. A deployment of your own that mounts files the gateway
must read (the registry, an identity map) needs them readable by UID 1000:
mode `0644`, or the same `keep-id` mapping.


## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `BEHEROUTER_GATEWAY_TOKEN` | **yes**, unless `AUTH_MODE=oidc` | Shared bearer token the gateway itself checks |
| `BEHEROUTER_REGISTRY` | yes | Path to `registry.toml` |
| `BEHEROUTER_PUBLIC_URL` | no | Published origin used by `client-config` output |
| `BEHEROUTER_<SURFACE>_*` | per backend | Backend credentials, referenced from the registry as `${VAR}` |
| `BEHEROUTER_ATTACH_TIMEOUT_S` | no | Per-surface attach bound in seconds, default `30`; a surface that exceeds it is served as `503` and retried |
| `BEHEROUTER_CALL_TIMEOUT_S` | no | Per-call bound on the backend call, unset = no limit; see [Logs, audit and metrics](#logs-audit-and-metrics) |
| `BEHEROUTER_LOG_FORMAT` | no | `text` (default) or `json` |
| `BEHEROUTER_AUDIT` | no | `on` (default) or `off` |
| `BEHEROUTER_AUDIT_CLAIMS` | no | Comma-separated JWT claim names to add to the audit line, default none |
| `BEHEROUTER_REGISTRY_WATCH_S` | no | Poll the registry (and every `${file:}` it names) every N seconds and [reload](#hot-reload) on change; unset or `0` = off |
| `BEHEROUTER_RELOAD_DRAIN_S` | no | Seconds a swapped-out surface keeps finishing its in-flight requests, default `30`; see [Hot reload](#hot-reload) |
| `BEHEROUTER_ADMIN_TOKEN` | no | Enables the [admin API](#admin-api); must differ from `BEHEROUTER_GATEWAY_TOKEN` |
| `BEHEROUTER_ADMIN_ROLE` | no | Enables the admin API for a verified JWT carrying this role; needs `AUTH_MODE` `oidc`/`both` and `BEHEROUTER_OIDC_ROLES_CLAIM` |
| `BEHEROUTER_KILLSWITCH_PATH` | no | Turns on the [kill switch](#kill-switch): the path of its JSON state file |

Secrets appear in `registry.toml` as `${VAR}` placeholders, expanded from the environment
at attach time. **They are never written into the registry**, so it is safe to commit.
A value that is exactly `${file:/absolute/path}` reads the secret from a file instead, which
is what lets a rotated secret reach a running gateway: see [Secrets from files](#secrets-from-files-and-rotation).

### Per-user identity (optional)

Unset, none of this applies and the gateway behaves exactly as it always has.
The mechanism, the five modes and the operating notes are in
[`IDENTITY.md`](IDENTITY.md); what a deployment needs to know:

| Variable | Required | Meaning |
|---|---|---|
| `BEHEROUTER_AUTH_MODE` | no (`shared`) | `shared` \| `oidc` \| `both`; `both` accepts a user JWT *beside* the shared token |
| `BEHEROUTER_OIDC_ISSUER` | with `oidc`/`both` | Checked as `iss` |
| `BEHEROUTER_OIDC_AUDIENCE` | with `oidc`/`both` | Checked as `aud` |
| `BEHEROUTER_OIDC_JWKS_URI` | with `oidc`/`both` | Explicit; **never discovered from the issuer** |
| `BEHEROUTER_OIDC_REQUIRED_SCOPES` | no | Comma-separated, gateway-wide |
| `BEHEROUTER_OIDC_ROLES_CLAIM` | with `[surface.authz]` | Dotted path, e.g. `realm_access.roles`; no default |
| `BEHEROUTER_IDENTITY_MAP` | with mode `lookup` | Default path to the mounted secret map |

⚠️ **Two configurations refuse to boot**, which is a dead gateway and `/healthz`
with it, so `registry-lint` checks both wherever these variables are visible: a
surface requiring a verified user while `BEHEROUTER_AUTH_MODE` is `shared`, and
a `require_roles` gate with no `BEHEROUTER_OIDC_ROLES_CLAIM`.

Mount the identity map **read-only**, and note that it is read on the call path
rather than at attach: a bad path degrades that one surface instead of killing
the gateway. `health --deep --json` reports its state per surface.

## The registry

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

Do not hand-write the surrounding plumbing. `beherouter plugin-config <surface> <plugin>`
emits the registry block, the reverse-proxy clause and the env line **from the plugin's own
spec**, so the three cannot disagree. It never emits a credential, only a placeholder.

See [`PLUGINS.md`](PLUGINS.md) for what a plugin is and how to write one.

## Pre-deploy gate

```bash
beherouter registry-lint --path registry.toml --json
```

Offline: no network, no attach. It turns a production-outage-shaped feedback loop into a
local one.

⚠️ **Run it against the image that is about to serve**, not against a workstation checkout.
Validating inside the new image means it checks the exact code that will run, against the
real environment, so `${VAR}` placeholders are **resolved rather than assumed**. Fail there
and the old container is still running and still serving — the bad registry never reaches
the service.

## Reverse proxy and auth

The gateway binds loopback. The proxy in front of it is what enforces **per-client** tokens,
keeping one client's surface away from another's; the gateway's own token is a single shared
credential and cannot make that distinction.

Configure **default-deny**, with an explicit allow per surface. Publishing the gateway on a
LAN interface would let anyone reach every surface with the one shared token, bypassing that
split entirely.

`/healthz` and `/metrics` are the only unauthenticated paths. `/metrics` carries counters only,
labelled by reason and never by caller. Leave it out of the edge's allow-list unless a scraper
needs it from outside.

`/admin` exists only when an admin credential is configured (see [Admin API](#admin-api)).
It is **not** unauthenticated, but it widens the network surface, so under default-deny
leave it out of every client's allow-list and allow it from the **admin network only**.
The chart renders no ingress path for it.

## Health and verification

```bash
# transport — is the process up, and what attached at startup?
curl -sS https://<your-gateway>/healthz
# {"status":"ok","surfaces":["office","plane"]}

# the credentials behind each surface — a real call per backend
beherouter health --deep --json      # exit 6 == a backend credential is bad

# per-user surfaces: the same probe AS a user (token from a file or stdin)
beherouter health --deep --surface plane --bearer-file - --json < token.txt

# call, session, surface and auth-rejection series
# (rejection reasons: expired | invalid | issuer | audience | scope)
curl -sS https://<your-gateway>/metrics

# the CLI contract, inside the image (not a host venv)
beheaxi conformance "beherouter"     # 6/6
```

`/healthz` answers HTTP 200 whenever the process is up. Its body carries `status`
(`ok`, or `degraded` when any surface failed to attach) and `surfaces`, plus keys that
appear only when they have something to say, so a clean gateway answers exactly
`{"status", "surfaces"}`:

| Key | Present when | Meaning |
|---|---|---|
| `failed` | a surface failed to attach, or its app died while serving | the surfaces answering `503`; transient faults are retried in the background |
| `needs_config_change` | a failure is a configuration fault (`UsageError`, e.g. a bad `[surface.identity]`) | the subset of `failed` the gateway has **stopped retrying**: it will not fix itself, so fix the registry, then reload (SIGHUP, `POST /admin/reload` or the watch) or restart; an env change needs a restart. That surface's `503` says so and carries no `Retry-After` |
| `pinned_missing` | a surface attached but its backend no longer serves some pinned tools | `{surface: [tool names]}`. The surface stays up and the status unchanged (logged at WARNING); `health --deep` fails it |
| `reload_failed` | a [reload](#hot-reload) changed a surface whose new entry failed to attach or start | the surfaces **still serving their pre-reload app**. Status stays `ok`: they are serving |
| `last_reload` | the last reload failed at lint | `{"status": "failed", "at": …}`; the whole old registry is still in force. Cleared by the next reload that passes lint |
| `disabled` | the [kill switch](#kill-switch) stops something | the quarantined surface names, or `["*"]` while `all` is set |
| `killswitch` | the state file is malformed | `"stale"`: the last good state is still enforced |

⚠️ **The kill switch does not make `status` `degraded`.** `degraded` means an attach failed;
a deliberate stop is not a fault. Alert on `disabled`, not on `status`.

⚠️ **An empty surface list is a valid state**, not an outage — it is what a gateway with no
registry entries correctly reports.

⚠️ **A green `/healthz` proves less than it looks like.** It proves the process is up and
which surfaces attached *at startup*. It cannot see a backend credential go bad — a revoked
token lists and searches its catalogue perfectly and fails only on a real call. That is
precisely why `health --deep` exists separately: it makes a **credentialed `tools/call`**
per surface.

`health --deep` also fails a surface whose pin list names a tool the backend no longer
serves (`catalogue: "pinned_missing"`). `/healthz` reports the same names under
`pinned_missing` from attach time, but leaves the status alone; without one of the two, a
pinned name that has disappeared is **silently not pinned**, and the only symptom is a short
`tools/list`.

### Scheduled deep health

Since `/healthz` cannot see a credential, run `health --deep` on a schedule and alert on
what it writes:

```bash
beherouter health --deep --textfile /var/lib/node_exporter/textfile_collector/beherouter.prom
```

`--textfile PATH` writes the sweep as a node_exporter **textfile-collector** file,
atomically, on a red sweep too (before exit 6), and never with an identity value in it.
Add `--bearer-file` with a dedicated monitoring identity's token to export the per-user
verdict as well (`mismatch` is the incident to page on). The metric contract, a systemd
timer, a Kubernetes CronJob and the alert rules — staleness included, because a sweep that
cannot run writes nothing — are in
[`contrib/health-textfile/`](../contrib/health-textfile/README.md). On Kubernetes the chart
does it for you: `healthCronJob.enabled=true` with `healthCronJob.textfile.hostPath` set
(render fails without it), see the chart README.

## Logs, audit and metrics

Four variables, all optional (an invalid value refuses boot):

| Variable | Default | Meaning |
|---|---|---|
| `BEHEROUTER_LOG_FORMAT` | `text` | `json` writes one object per line: `ts` (ISO 8601, local offset, so `TZ` is honoured), `level`, `logger`, `msg`, structured fields, and any exception as an `exc` string. Never multi-line |
| `BEHEROUTER_AUDIT` | `on` | `off` disables the audit line |
| `BEHEROUTER_AUDIT_CLAIMS` | empty | Claim names added to `caller`, read from the verified JWT only |
| `BEHEROUTER_CALL_TIMEOUT_S` | unset | Per-call limit in seconds; unset = no limit |

Log levels: a successful call logs at DEBUG while the audit is on (the audit line
records it) and at INFO when `BEHEROUTER_AUDIT=off`; a refusal, `backend_rejected`, `unknown_tool` and `bad_arguments` log one
WARNING line; `backend_unavailable` and `timeout` one ERROR line without a traceback;
`internal` an ERROR with its traceback. The `beherouter.calls` line omits the error text
for `unknown_tool` and `bad_arguments`, because those messages are built from caller input.

### The audit line

One JSON object per call on the `beherouter.audit` logger, always JSON, written to stdout
whatever `BEHEROUTER_LOG_FORMAT` says. It covers pinned tools, `run_tool` and the
meta-tools.

```json
{"ts":"2026-10-08T10:12:03.412+02:00","event":"tool_call","call_id":"6f1c…","surface":"dwh","tool":"run_tool","inner_tool":"list_tables","caller":{"sub":"7d2e…","email":"a@example.com"},"auth":"oidc","outcome":"ok","reason":null,"status":null,"latency_ms":31012}
```

| Field | Meaning |
|---|---|
| `call_id` | random UUID4, also on the call's log line, so the two join |
| `tool`, `inner_tool` | the published tool; for `run_tool`, the tool it ran (else `null`) |
| `caller` | `sub` from the verified token plus the `BEHEROUTER_AUDIT_CLAIMS` claims. A shared-token call is `{"sub": null}` |
| `auth` | `oidc`, `shared` or `none` |
| `outcome`, `reason`, `status` | how it ended (`ok` or a kind below), the reason enum below, and the upstream HTTP status when the backend gave one (an HTTP backend's or an OpenAPI upstream's 4xx/5xx; `null` otherwise — an MCP `isError` result carries no status, and its text is never parsed for one) |
| `latency_ms` | gate to result |

**Arguments are never recorded**, and there is no switch to record them. That includes the
calls FastMCP refuses before a tool function runs: arguments that fail the published schema
(`run_tool` with `args` sent as a JSON string, say) and a name the surface does not publish.
Each is still one audited, counted call with `_meta` (`bad_arguments`, naming the failing
field but never its value; `unknown_tool`, labelled `tool="<unknown>"`). FastMCP's own
WARNING for a schema failure keeps the tool name and shows the details as `<redacted>`.

An [admin API](#admin-api) request writes a different line, `"event": "admin"`, on the same
logger (see there).

### Metrics

`GET /metrics` is unauthenticated.

| Series | Type | Labels |
|---|---|---|
| `beherouter_tool_calls_total` | counter | `surface`, `tool`, `outcome` |
| `beherouter_tool_call_duration_seconds` | histogram | `surface`, `tool` |
| `beherouter_active_sessions` | gauge | `surface` |
| `beherouter_surface_up` | gauge | `surface` (1 attached, 0 serving 503) |
| `beherouter_auth_rejections_total` | counter | `reason`, `surface` |
| `beherouter_reloads_total` | counter | `trigger` (`sighup`/`admin`/`watch`), `outcome` (`ok`/`partial`/`failed`) |
| `beherouter_reload_last_success_timestamp_seconds` | gauge | none (the last reload whose lint passed, `ok` or `partial`) |
| `beherouter_surface_disabled` | gauge | `surface` (1 while the kill switch stops it; 1 for every surface while `all` is set) |
| `beherouter_blocked_subjects` | gauge | none (a count, never a name) |

A removed surface's `surface_up`, `active_sessions` and `surface_disabled` gauges are removed;
its counters and histograms stay. No new label carries a caller.

`tool` is the inner tool for `run_tool`; an unknown name is `tool="<unknown>"`, so
cardinality is bounded by the catalogue. A `run_tool` refused at the identity gate never
reaches its inner name, so it is labelled `tool="run_tool"`. `surface` on rejections is `""` when the request
path names no configured surface. Counters reset on restart, as all Prometheus counters
do: use `rate()` and `increase()`. `beherouter_active_sessions` reads a private FastMCP
attribute; if a FastMCP upgrade removes it the series is omitted, with a WARNING at
startup, never faked.

### Error results

A failed call returns `isError: true`. The text is the human sentence; the machine-readable
part is `_meta["io.beherouter/error"] = {type, code, reason, context}` (`type` and `code`
from the beheaxi envelope). `context` names what was *required*, never what the caller had.

| `reason` | Outcome | When | `context` |
|---|---|---|---|
| `unauthenticated` | `refused` | no usable token | none |
| `missing_role` | `refused` | `require_roles` gate | `required_roles`, `missing_roles` |
| `wrong_audience` | `refused` | `[surface.authz] audience` | `expected_audience` |
| `rate_limited` | `refused` | `[surface.rate_limit]` exhausted | `retry_after_s` (int, rounded up), `limit` (e.g. `"60/60s"`) |
| `confirmation_required` | `refused` | `confirm_mutating` not satisfied | `confirmation`: `unsupported`, `declined` or `timeout` |
| `surface_disabled` | `refused` | the kill switch stops the surface | `scope`: `all` or `surface` |
| `caller_blocked` | `refused` | the kill switch blocks the caller's `sub` | none |
| `identity_unavailable` | `unavailable` | lookup map or exchange failure | none |
| `unknown_tool` | `not_found` | name not in the catalogue | `suggestions` |
| `bad_arguments` | `tool_error` | arguments failed the schema or argument preparation | none |
| `edition_unsupported` | `tool_error` | backend guard (Plane CE) | none |
| `backend_rejected` | `tool_error` | backend refused: an MCP `isError` result, or an HTTP 4xx | `status` when known |
| `backend_unavailable` | `unavailable` | backend HTTP 5xx or transport failure | `status` when known |
| `timeout` | `timeout` | `call_timeout_s` expired | `limit_s` |
| `internal` | `internal` | a beherouter bug (logged with traceback) | none |

### Rate limits

```toml
[plane]
plugin = "plane"
  [plane.rate_limit]
  calls = 60        # tokens per window
  per_s = 60        # window, seconds
  burst = 10        # bucket size; defaults to `calls`
```

A token bucket per caller. It counts only calls that **reach the backend**
(pinned tools and `run_tool`); `search_tools`, `describe_tool` and `context_cost`
are free. The key is the OIDC `sub`; every shared-token or anonymous caller shares
one bucket, `<shared>`. A refusal is `rate_limited` with `retry_after_s` and
`limit`. It runs after the per-tool role gate and before confirmation, outside
`call_timeout_s`.

⚠️ **Limits are per replica and live in memory.** N replicas allow up to N times
the limit, and a restart refills every bucket. It is a courtesy against a runaway
agent, not quota enforcement.

**A [reload](#hot-reload) carries the limiter over, per surface.** A changed surface whose
`[rate_limit]` table is equal keeps its limiter (its buckets and their fill), so a token
rotation does not hand every caller a fresh burst. A changed table starts with full buckets;
a removed surface's limiter is dropped.

### Call timeout

`call_timeout_s` on a registry entry, falling back to `BEHEROUTER_CALL_TIMEOUT_S`; unset
means no limit, so an upgrade cannot cut off a slow backend. It bounds the backend call,
including a pending token exchange and the backing's own guard (Plane's edition check); the
gateway's identity gate and the identity resolution before the call (a lookup-map read) run
outside it. A `cli` verb's subprocess is killed when the limit expires. On expiry the call ends with
`reason: "timeout"` and `context.limit_s`. A bad env value refuses boot; `registry-lint`
refuses a non-numeric or non-positive entry value. `plugin-config` does not emit the key.

## Hot reload

Re-read `registry.toml` and re-attach **only the surfaces that changed**, without a restart.
Three triggers, all opt-in or free:

```bash
kill -HUP <gateway pid>                       # sighup: fire and forget, the outcome is logged
curl -X POST -H "Authorization: Bearer $ADMIN" https://<gateway>/admin/reload   # admin: waits, returns the result
BEHEROUTER_REGISTRY_WATCH_S=30                # watch: poll the registry and every ${file:} path
```

The watch stats the registry and each `${file:…}` path by `(mtime_ns, size, inode)`, following
symlinks, so the atomic symlink swap of a Kubernetes ConfigMap or Secret volume is seen. A
registry that fails lint is reported **once**, not on every tick, and an edit that lands
during a reload triggers another one. SIGHUP is installed only in the process's main thread.

Reloads are serialized. Requests that arrive during one **coalesce** into exactly one
follow-up reload, and each of them receives its result. After a gateway shutdown has begun, a
request returns `failed` (to an admin caller, `503`).

**What a reload does**, per surface (a fingerprint over the entry and the resolved value of
every placeholder, so a rotated `${file:}` secret changes only its own surface):

| Case | Result |
|---|---|
| lint fails (bad TOML, a reserved name, an unset `${VAR}`, an unreadable `${file:}`) | **nothing changes**: the whole old registry stays in force; logged at ERROR; `/healthz` `last_reload`; `422` to an admin caller |
| unchanged | not touched: same app, same sessions, same rate limiter |
| added, attached | live |
| added, failed | `503` and retried in the background, or `needs_config_change` for a configuration fault, as at boot |
| changed, attached | the app is swapped; the old one drains and closes |
| changed, failed to attach **or to start** | **the old app keeps serving**; listed under `reload_failed`; a retry for the new entry swaps it in when it attaches (a configuration fault stops the retry). A reload never turns a working surface into a `503` |
| removed | the path answers `404` at once; the old app drains and closes; its gauges are removed |

The result is `{trigger, outcome, added, changed, removed, unchanged, failed}`, names only;
when `outcome` is `failed` it also carries `error` (the admin API returns it as the 422
problem detail).
`outcome` is `ok`, `partial` (an added or changed surface failed) or `failed` (lint). It is
logged (INFO, WARNING, ERROR) and counted in `beherouter_reloads_total`.

**The drain.** A swapped-out or removed app takes no new requests at once and keeps finishing
the ones in flight for `BEHEROUTER_RELOAD_DRAIN_S` (default 30). Then it is cancelled and its
backend closed; the deadline exists because a streaming-HTTP GET never ends by itself.
**A changed surface's MCP sessions end at the swap** (they live in the old app), and clients
re-initialize against the new app, whose `tools` array freezes anew — unless the surface is [stateless](#stateless-sessions), which has no session to end. An unchanged surface
keeps its sessions and its hosts' prompt caches.

⚠️ **What a reload cannot change.** Gateway-wide settings are process environment, read once,
and stay restart-only: `BEHEROUTER_AUTH_MODE`, the OIDC and JWKS settings, the shared client
token, the admin variables, the audit and log settings, the timeouts, and the kill-switch,
watch and drain settings. A SIGHUP does not rotate the shared client token. `${VAR}`
placeholders re-resolve on a reload but read an environment that never changes; `${file:}`
is the rotation path.

Kubernetes: `hotReload.enabled=true` sets `BEHEROUTER_REGISTRY_WATCH_S` from
`hotReload.watchSeconds` (default 30) and drops the `checksum/configmap-registry`
annotation, so a ConfigMap edit is applied in place instead of rolling the pods. Leave it
off and an edit rolls the pods, as before.

⚠️ **Under `scripts/deploy.sh` a reload does not pick up a registry edit**: the container
mounts a per-deploy snapshot, so a registry change still means re-running `deploy.sh`
([§ Hot reload, secret files and the kill switch under `deploy.sh`](#hot-reload-secret-files-and-the-kill-switch-under-deploysh)).

## Secrets from files and rotation

A registry value that is **exactly** `${file:/absolute/path}` is replaced by the file's
content:

```toml
[plane.env]
api_key = "${file:/run/secrets/beherouter/plane_api_key}"
```

- It is read at `registry-lint`, at boot, at attach and on each reload, never per call.
- **Exactly one trailing newline is stripped** (Kubernetes Secrets and `echo >` add one).
- A relative path, or a missing, unreadable or empty file, refuses with a message that names
  the **path**, never the content. Nothing logs the value; `registry-lint` reports only
  that the file exists and is non-empty.
- ⚠️ A bind-mounted file must be **readable by UID 1000**, like every file the gateway reads.

Rotation without a restart: replace the file, then reload (or let the watch notice). Only
the surfaces whose resolved value changed re-attach. On Kubernetes use the chart's
`secretFiles`, which mounts an existing Secret **as a directory** (`/run/secrets/beherouter`
by default). ⚠️ Never `subPath`: kubelet updates a projected directory in place but never a
`subPath`'d file, and rotation without a restart is the whole point. The mount is in the
Deployment, the registry-lint hook Job and the health CronJob, and the hook runs
pre-install, so the **Secret must exist before `helm install`**; the chart does not create it.
Under `scripts/deploy.sh`, mount the secret file's directory with `--volume`
([§ … under `deploy.sh`](#hot-reload-secret-files-and-the-kill-switch-under-deploysh)).

## Stateless sessions

By default a surface's MCP sessions live in the gateway process. A rollout or a reload that
changes the surface ends them, and every replica needs client affinity
(`service.sessionAffinity: ClientIP`). `stateless = true` on an entry serves that surface with
FastMCP's stateless streamable-HTTP app instead: no `mcp-session-id`, every request
self-contained, so any replica can answer any request and nothing is lost at a swap.

```toml
[plane]
plugin = "plane-http-apikey"
stateless = true
```

**What it costs:**

- **No `confirm_mutating`.** Confirmation asks the user through MCP elicitation, which needs a
  session; the pair is refused by `registry-lint`, at boot and by a reload.
- **No `beherouter_active_sessions` series** for the surface (there is nothing to count).
- **No server push.** A `GET` answers 405 (before authentication: an unauthenticated `GET` gets 405,
  not 401, and is not counted in `beherouter_auth_rejections_total`), so
  `notifications/tools/list_changed` cannot reach the client. The published `tools` array is frozen per app anyway; a client sees a changed one
  when it rebuilds its connection.
- Rate limits stay in memory, per replica ([§ Rate limits](#rate-limits)), and the kill-switch
  file still needs a volume every replica shares.

**LibreChat** (v0.8.7, MCP SDK 1.29.0; spike 2026-10-09) works unchanged: it initializes once
per connection, takes the 405 as "no SSE stream" without logging it, and sends only
`tools/call` on later turns. Restarting or rolling the gateway between two calls is invisible
to it; the same rolling update against a stateful surface fails the next tool call with
`404 Session not found` before LibreChat reconnects. No client-config change is needed.

To scale out, make every surface stateless; then `sessionAffinity` and `replicaCount: 1` are
no longer needed (a stdio backend still runs one subprocess per replica). With replicas, reload every pod
(`BEHEROUTER_REGISTRY_WATCH_S` or a SIGHUP per pod): `POST /admin/reload` through a Service
reaches only one pod.

## Kill switch

Stop every surface, quarantine one, or block one caller, effective on the **next call** with
no restart. Off unless `BEHEROUTER_KILLSWITCH_PATH` names a state file.

```json
{"all": {"reason": "incident 42", "at": "2026-10-09T10:00:00+00:00", "by": "<actor>"},
 "surfaces": {"dwh": {"reason": "quarantined", "at": "…", "by": "…"}},
 "subjects": {"user-123": {"reason": "offboarded", "at": "…", "by": "…"}}}
```

`all` is an object or absent; `at`, `by` and `reason` are optional when a human edits the
file. A missing file is empty state (a file that vanishes while it held a stop or block
logs one WARNING, with counts only). The gateway stats the file on every call and re-parses
only when `(mtime_ns, size, inode)` changes, so a hand edit, a ConfigMap, a Secret or a
shared volume takes effect at once, with no reload. The file holds caller `sub` values: they
leave it only through the admin API, never into a log, metric, `/healthz` or refusal text.
The one exception is not the kill switch's: a blocked caller's **own** refused call writes its
`tool_call` audit line with its `sub`, as every caller's call does (§ The audit line).

**Enforcement** is a stage ahead of every call, pinned tools and meta-tools alike, checked in
this order: `all` (`surface_disabled`, `scope: all`), then the surface
(`surface_disabled`, `scope: surface`), then the caller's verified `sub` (`caller_blocked`).
A blocked caller on a stopped surface learns only that the surface is stopped. The refusal
is a tool-level `isError` result, not an HTTP 401/403 (LibreChat reads a 401 as "OAuth
required"); `initialize` and `tools/list` still answer, so a quarantined surface reads as
switched off, not broken. The text names the category, never the stored `reason` or the `sub`.
A shared-token caller has no `sub` and is stopped by surface or `all` only.

⚠️ **A malformed file keeps the last good state** (an ERROR once per distinct file stamp,
`/healthz` `killswitch: "stale"`): failing open on a typo would silently lift every block,
failing closed would make a typo a total outage. **At boot there is no last good state, so a
malformed file refuses boot**, and `registry-lint` validates the file when the variable is set.

⚠️ **`health --deep` is not stopped by the kill switch**: it calls the backend directly, as it
does for the other gates. Deliberate.

⚠️ **Several pods writing one shared file can race**; the last writer wins. Each write
re-reads the file first, so a human edit between two API writes is kept. Use a ReadWriteMany
volume when `replicaCount` > 1, or replicas disagree about who is blocked.

Drive it with the [admin API](#admin-api):

```bash
ADMIN=…   # the admin token (or an admin-role JWT)
G=https://<gateway>/admin/killswitch
curl    -H "Authorization: Bearer $ADMIN" $G                                    # read the state
curl -X PUT    -H "Authorization: Bearer $ADMIN" -d '{"reason":"incident 42"}' $G/all   # stop everything
curl -X DELETE -H "Authorization: Bearer $ADMIN" $G/all                          # resume
curl -X PUT    -H "Authorization: Bearer $ADMIN" -d '{"reason":"quarantine"}' $G/surfaces/dwh
curl -X DELETE -H "Authorization: Bearer $ADMIN" $G/surfaces/dwh                 # release it
curl -X POST   -H "Authorization: Bearer $ADMIN" -d '{"sub":"user-123","reason":"offboarded"}' $G/subjects/block
curl -X POST   -H "Authorization: Bearer $ADMIN" -d '{"sub":"user-123"}' $G/subjects/unblock
```

A `sub` travels **in the body, never the path**, so it never lands in a proxy's access log.
`reason` is optional, at most 200 characters, stored in the file and never logged. An unknown
surface name is accepted with `"warning": "no such surface"`, so you can quarantine a surface
before it exists. Every write answers `{"state": …}` with the new state.

Chart: `killswitch.enabled` with `killswitch.existingClaim` (required) mounts the claim at
`killswitch.mountPath` and sets `BEHEROUTER_KILLSWITCH_PATH=<mountPath>/killswitch.json`. The
pod gets `fsGroup: 1000` unless `podSecurityContext` sets one. The claim is mounted on the
Deployment only (not the hook Job or CronJob). Under `scripts/deploy.sh`, mount a writable
directory with `--volume`
([§ … under `deploy.sh`](#hot-reload-secret-files-and-the-kill-switch-under-deploysh)).

## Admin API

`POST /admin/reload` and the kill-switch routes above. **Off by default: with neither
credential configured there is no `/admin` route** (404), so an upgrade adds no network
surface. The kill-switch routes additionally need `BEHEROUTER_KILLSWITCH_PATH`.

Send `Authorization: Bearer <credential>`. Either passes:

- `BEHEROUTER_ADMIN_TOKEN`, compared in constant time. ⚠️ Boot is refused if it equals the
  clients' shared gateway token: one secret must not do two jobs.
- A JWT the gateway's verifier accepts (JWKS, issuer, audience) whose roles claim contains
  `BEHEROUTER_ADMIN_ROLE`. Boot is refused unless `BEHEROUTER_AUTH_MODE` is `oidc` or `both`
  and `BEHEROUTER_OIDC_ROLES_CLAIM` is set. The shared client token is never an admin credential.

| Status | When |
|---|---|
| `200` | done. A reload returns its result; a kill-switch write returns `{"state": …}` (plus `"warning"` for an unknown surface); `GET /admin/killswitch` returns the state object itself, unwrapped |
| `400` | bad JSON, a wrong shape, an over-long `reason`, a missing/over-long `sub`, or a write that would leave an invalid state file |
| `401` | no credential, or one that is not an admin credential |
| `403` | a valid JWT without the admin role |
| `404` | no admin credential configured, or the kill-switch routes without a state file |
| `409` | the state file is read-only (EROFS/EACCES) or malformed: edit it where it is managed |
| `422` | a reload whose registry failed lint (only that); the old registry stays in force |
| `503` | a reload requested after shutdown has begun, or cancelled by it, or failing unexpectedly (logged at ERROR) |

Every body is an RFC 9457 problem, except the `401` for an **expired** admin JWT, which carries
the RFC 6750 `error_description="token expired"` like a surface's does. Refusals count in
`beherouter_auth_rejections_total` with `surface=""`: reason `missing` (no bearer), `invalid`,
`expired` etc. from the verifier, and `missing_role` (the `403`); `missing` and `missing_role`
are seeded at zero.

**Every admin request writes one audit line**, refusals and reads included
(`BEHEROUTER_AUDIT=off` silences them):

```json
{"ts":"…","event":"admin","action":"block_subject","target_kind":"subject","target":"sha256:3f2a9c1e","actor":"<admin-token>","outcome":"ok"}
```

`action` is one of `reload`, `read_killswitch`, `stop_all`, `resume_all`, `disable_surface`,
`enable_surface`, `block_subject`, `unblock_subject`. `target` is the surface name, `*` for
`all`, `-` for a reload or read, and for a subject the first 8 hex of the SHA-256 of the
`sub`: enough to match a block to its unblock, never the value. `actor` is `<admin-token>`,
the admin JWT's subject, `<unknown-subject>` for an admin JWT without a `sub`, or `<refused>`
for a rejected request. `outcome` is `ok`, a
reload outcome (`partial`, `failed`), `refused`, `bad_request` or `conflict`. No body, no
`reason`, no credential.

Chart: `admin.token` becomes the chart Secret's `BEHEROUTER_ADMIN_TOKEN`; together with
`secret.create=false` it **fails the render** (supply the token through `extraEnv` from your
own Secret instead); `admin.role` sets `BEHEROUTER_ADMIN_ROLE`. No ingress path is rendered for `/admin`.

## Adding or removing a surface — three edits, not one

| Edit | Miss it and… |
|---|---|
| The `registry.toml` block | — |
| The reverse-proxy allow clause | the surface is unreachable (default-deny) |
| The per-client token in the env | it answers to **another client's token** |

Set `probe` from the start so `health --deep` can prove the credential.

⚠️ **An entry may not be committed before its secret exists.** An unset or empty `${VAR}`
raises during startup, so it does not yield a broken surface — it yields a **dead gateway**,
`/healthz` included. Entry and secret ship together, or not at all.

⚠️ **A stale proxy clause is worse than a missing one.** Remove a surface from the registry
but leave its allow clause, and the retired token still passes the edge — and would
authorize that old token against whatever surface later claims the same path.

## Upgrading

1. Get the new image: published releases already exist
   (`ghcr.io/behemotion/beherouter:<version>`); a self-built image is the only
   case where this step means "rebuild".
2. Run the pre-deploy gate **inside the new image**.
3. Restart.
4. Verify `/healthz` **and** `health --deep`.

Ordering matters: build **before** switching config or restarting. A build that fails after
the container has already been restarted against a new registry leaves the gateway
crash-looping on a missing dependency — taking every surface and `/healthz` down.

## Rollback

Run the previous image against the previous registry. There is **no state to migrate**.
`scripts/deploy.sh` does this automatically when verification fails (below); by hand, it is
a redeploy of the older tag:

```bash
podman logs --tail 200 beherouter        # what actually failed
scripts/deploy.sh --tag <previous version> --deep
```

## Single-host deploy with `scripts/deploy.sh`

On one podman host, [`scripts/deploy.sh`](../scripts/deploy.sh) is the
upgrade procedure above as one command — and it adds the step a hand-run
upgrade usually skips: **automatic rollback**.

```bash
# the release matching this checkout's pyproject version, verified end to end
scripts/deploy.sh --registry /srv/beherouter/registry.toml \
                  --env-file /srv/beherouter/.env --deep

scripts/deploy.sh --tag 0.2.5 --deep      # a specific published release
scripts/deploy.sh --build --deep          # build localhost/beherouter:<version> from this checkout
scripts/deploy.sh --dry-run ...           # print every command; change nothing
```

| Step | What it does | On failure |
|---|---|---|
| 1. image | `podman pull` the published image (or `--build` it; `--no-pull` uses a local one) | exit; nothing touched |
| 2. pre-deploy gate | snapshots the registry, then `registry-lint` **inside the new image** with the env file it will serve with | exit; the running gateway is untouched |
| 3. switch | records the running container's image, stops it and keeps it as `<name>-previous`, starts the new one | rollback |
| 4. verify | polls `/healthz`: `ok` passes, `degraded` **fails** unless `--allow-degraded`; with `--deep`, `beherouter health --deep --json` in the new container must exit 0 | rollback |
| 5. cleanup | removes `<name>-previous` (kept with `--keep-previous`) | — |

**Rollback** removes the new container (its last 50 log lines go to stderr
first), renames `<name>-previous` back, starts it and checks `/healthz`
again; the script then exits 1. Ctrl-C after the switch rolls back too.

⚠️ **Each deploy mounts a read-only snapshot of the registry**
(`<registry dir>/.deploy/registry.<id>.toml`, `--state-dir` to move it), not
the file itself. The gateway re-reads its registry at every start, so a
rollback onto a shared, since-edited file would restart the *old* image on the
*new*, failing config. To change the registry, edit the source file and re-run
`deploy.sh`, which puts every registry change through the lint gate. The env
file needs no snapshot: podman copies it into the container when it creates it.

### Hot reload, secret files and the kill switch under `deploy.sh`

The registry snapshot is **by design** (it is what makes a rollback restore the old config),
so SIGHUP, `POST /admin/reload` and the watch re-read an unchanged file: **a registry change
still means re-running `deploy.sh`**. Everything else works through the repeatable
`--volume SRC:DST[:OPTS]`, which is given to **both** the lint run in the new image and the
gateway container, so the gate sees what the gateway will:

```bash
scripts/deploy.sh --tag 0.2.5 \
  --volume /srv/beherouter/secrets:/run/secrets/beherouter:ro,z \
  --volume /srv/beherouter/killswitch:/var/lib/beherouter:z
# .env: BEHEROUTER_KILLSWITCH_PATH=/var/lib/beherouter/killswitch.json
```

- **`${file:}` secrets**: mount the directory read-only; rotate by replacing the file, then
  `POST /admin/reload` (or set `BEHEROUTER_REGISTRY_WATCH_S`). Without the mount,
  `registry-lint` refuses a registry that references the file, and the deploy stops at step 2.
- **The kill switch**: mount a **writable directory** (writes are an atomic rename beside the
  file, so a single-file mount is not enough), owned by or writable for UID 1000.
- `--volume` refuses a destination of `/data` or `/data/registry.toml`: the registry comes
  only from `--registry`.

⚠️ **`--deep` is the meaningful check.** `/healthz` cannot see a revoked
backend credential (see § Health and verification); `health --deep` makes a
real call per surface. Use it whenever the surfaces have a `probe`.

Defaults match `podman-compose.yml`: registry `./data/registry.toml`, env
file `./.env`, container `beherouter`, published on `127.0.0.1:47100`
(`--publish ADDR:PORT`; keep it loopback, the reverse proxy is the network
surface). `--network NET` joins a podman network that same-host backends are
reachable on by name. Under rootless podman the container runs with
`--userns keep-id:uid=1000,gid=1000`, as compose does; under rootful podman,
make the registry readable by UID 1000 (`0644`).

The env file is podman's `--env-file` format: `KEY=value` per line, **no
quoting** — quotes become part of the value, and a quoted gateway token is a
401 that looks like a bad token.

The container is created with `--restart=always`. To bring it back after a
reboot, enable podman's restart service once:
`systemctl --user enable --now podman-restart.service` (rootless; with
`loginctl enable-linger $USER`) or `systemctl enable --now
podman-restart.service` (rootful). Do not also run it under
`podman-compose` — the two would fight over the name and the port.

Exit status: `0` deployed and verified; `1` failed (rolled back where there
was a previous container); `2` usage error.

## Deploying on Kubernetes (Helm)

The chart lives at [`charts/beherouter`](../charts/beherouter) — one Deployment,
no state; every rule on this page has a direct translation there, and the chart
README carries the values reference.

```bash
helm install beherouter oci://ghcr.io/behemotion/charts/beherouter \
  --namespace beherouter --create-namespace \
  --set-string secret.gatewayToken="$(openssl rand -hex 32)"
```

That is the whole install: **chart and image are both published** — the chart as
an OCI artifact pushed once per release and never overwritten, the image as its
own `appVersion` default (`ghcr.io/behemotion/beherouter:<chart appVersion>`) —
and an empty registry is a valid parked gateway. Installing from a checkout
(`helm install beherouter charts/beherouter`) is equivalent and is what CI
exercises. `prod-values.yaml` then carries the one required value —
`secret.gatewayToken` — plus `registry:` and one `secret.env` key per `${VAR}`
it names; render fails loudly without the token, so an install cannot
half-happen.

How the chart holds this page's rules:

| Rule on this page | What the chart does |
|---|---|
| Pre-deploy gate **inside the image that is about to serve** | a pre-install/pre-upgrade **hook Job**: same image, same env, same registry text; lint failure fails the release before anything is created |
| Entry and secret ship together | `required` at render time — no token, no release; an unset `${VAR}` fails the hook, not a live surface |
| The proxy is the **per-client** token boundary | the Ingress routes only; per-client auth is whatever fronts it (forward-auth, Gateway filters). The shared in-app token cannot make that distinction here either |
| `/healthz` (and `/metrics`) the only unauthenticated paths | startup/liveness/readiness probes all hit it; `helm test` asserts its payload |
| Loopback bind, only the edge reaches in | optional NetworkPolicy: default-deny ingress except the sources you list |
| A surface that fails to attach is isolated: `503` for it, `/healthz` `"degraded"` (still HTTP 200) | the probes check only for a 200, so a degraded pod **passes them and the rollout proceeds**; ⚠️ `helm test` asserts `"status":"ok"` and **fails on a degraded gateway** — intended, run it after every upgrade. A registry mistake still fails the lint hook. Surfaces attach concurrently, so boot costs the slowest attach, bounded by `BEHEROUTER_ATTACH_TIMEOUT_S` |
| `/healthz` cannot see a revoked credential | opt-in `healthCronJob`: `health --deep --textfile` on a schedule, with the gateway's env, Secret, registry, plugins and CA bundle; exit 6 (a red sweep, already in the file) counts as Job success, so a failed Job means the sweep could not run |
| The gateway itself fails to start (an import error, a bad auth setting) | `maxUnavailable: 0` rolling update — the failing NEW pod stalls the rollout while the **previous revision keeps serving**; `helm rollback` back, no state to migrate |

Upgrade is `helm upgrade` (the lint hook re-runs first); rollback is `helm
rollback`. Deep verification stays the same command, run against the Deployment:

```bash
kubectl exec deploy/beherouter -- beherouter health --deep --json   # exit 6 == bad credential
```

### Private CA, out-of-tree plugins

- **`caBundle`**: use it when your IdP (JWKS over TLS) or a backend sits behind
  a private CA. An init container appends the PEMs from a ConfigMap to the
  system bundle, and the gateway, its stdio children and the lint hook read
  the result through `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`. Without it,
  every JWT fails verification while the shared token stays green, so
  `health --deep` does not show the failure.
- **`plugins.install`**: installs `beherouter.plugins` distributions into an
  emptyDir on `PYTHONPATH`, in the gateway's own environment, before the
  gateway **and** the lint hook start. Dependencies the gateway already has are
  pinned to its versions and then pruned from that directory. A plugin that
  needs a different `httpx` or `fastmcp` fails its init container rather than
  shadowing the gateway's copy (`python -m beherouter.plugininstall`).
  `plugins.indexUrl` replaces PyPI as the default index.
- **`plugins.indexes[]`**: more named indexes, for a plugin published to a
  second registry (a GitLab project's package registry, say):

  ```yaml
  plugins:
    install: ["beherouter-back-office==0.1.0"]
    indexes:
      - name: back-office           # [a-z0-9-]+, never "plugins"
        url: https://gitlab.example.com/api/v4/projects/1234/packages/pypi/simple
        credentialsSecret: gitlab-back-office   # keys: username, password
  ```

  They become uv's `UV_INDEX` and are searched **before** the default index.
  uv takes a package from the first index that has it, so a same-named public
  package cannot replace a private one. The chart never changes that
  strategy. Each Secret reaches only the init container, as
  `UV_INDEX_<NAME>_USERNAME`/`_PASSWORD` (upper-cased, `-` → `_`), and never
  appears in a URL.
- **`plugins.local`**: wheels from a ConfigMap, for a plugin that has no index
  at all:

  ```bash
  kubectl create configmap dwh-wheels --from-file=beherouter_dwh-0.3.0-py3-none-any.whl
  ```
  ```yaml
  plugins:
    local:
      configMap: dwh-wheels
      wheels: ["beherouter_dwh-0.3.0-py3-none-any.whl"]
  ```

  The ConfigMap must stay under 1 MiB in total. Wheels only: a plugin
  registers through entry-point metadata, and a bare `.py` file has none. The
  wheel is installed by the same installer with the same pinning and pruning,
  and its own dependencies still come from the indexes. **Name every wheel
  with its version and list it.** The new filename changes the pod template,
  so `helm upgrade` rolls the pods. Editing the ConfigMap alone rolls nothing.
  The ConfigMap is mounted `optional`. If it is missing, or does not contain a
  listed file, the `plugins` init container fails with
  `plugin wheel not found: /etc/beherouter/plugins-local/<configMap>/<file>`
  and the pod does not wait in `ContainerCreating`. The lint hook installs the
  same wheels, so a missing one also stops `helm upgrade`.

  `plugins.install`, `plugins.indexes` and `plugins.local` can be combined.
  They share one init container, which renders when `install` or
  `local.wheels` is non-empty. For anything the chart does not model, use
  `extraInitContainers` / `extraVolumes` / `extraVolumeMounts`, which reach
  both pods too.

### Materialising a `stdio` backend at pod start (a `cmd` override)

The published image ships `plane-mcp-server` for the `plane` plugin, so this is
no longer needed for it. For any **other** stdio server that is not in the
image, you can materialise it at start-up by overriding `cmd` in the registry
entry. Shown here with Plane, the case it was first measured on:

```toml
[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "acme"
  cmd = "uv run --exclude-newer 2026-09-01 --with plane-mcp-server==0.3.2 plane-mcp-server stdio"
```

⚠️ **This trades a build-time dependency for a runtime one.** A PyPI outage or
an egress change then presents as a surface answering `503` and retrying at every
pod start, rather than as a failed build. Baking the server into a derived image is the sturdier choice; what
follows is what the override needs when you take it anyway (all four verified in
production by an operator running it under a hardened security context):

| Requirement | Why |
|---|---|
| `HOME` and `UV_CACHE_DIR` pointed at a writable path — on the chart, `extraEnv` onto the `/tmp` emptyDir it already mounts | with `readOnlyRootFilesystem: true` and neither set, the gateway crash-loops with **no clear error** |
| `--exclude-newer <date>` beside the `==` pin | `plane-mcp-server==0.3.2` pins only the top level; without a date pin its ~80 transitive dependencies re-resolve on **every pod start**, so two pods restarted weeks apart run different code |
| Nothing else on stdout | `uv` writes progress to **stderr only**, which is why this works at all: the MCP stdio channel on stdout stays uncorrupted |
| Accept two FastMCP versions in one pod | `uv run --with` builds an isolated environment, so the backend can hold a different FastMCP than the gateway (3.2.0 beside 3.4.5, observed) — a feature here, not a conflict |

```yaml
# values.yaml — the two variables, onto the emptyDir the chart already mounts
extraEnv:
  - name: HOME
    value: /tmp
  - name: UV_CACHE_DIR
    value: /tmp/uv-cache
```

Under `runAsNonRoot` + UID 1000 + all capabilities dropped +
`automountServiceAccountToken: false`, a full JSON-RPC `initialize` through such
a backend succeeds.

⚠️ A `stdio` backend can never carry a per-request identity, however it is
materialised — see [`IDENTITY.md`](IDENTITY.md) §7.

---

## Appendix — failure modes worth knowing before you hit them

These were learned on a homelab reference deployment (rootless Podman + a systemd user unit
+ `loginctl enable-linger` + Caddy) that ran from 2026-08 until 2026-10. That deployment is
retired and older documents in `docs/superpowers/` and `docs/handoffs/` still describe it;
the warnings below are the part that applies to any podman host.

**Reboot survival is observed, not assumed.** With linger enabled, user units and
`podman-restart.service` start at boot with nobody logged in. Verify it by rebooting and
checking container uptime against host uptime, rather than trusting the unit file.

⚠️ **A surface that fails to attach no longer takes the gateway down — but look for it.**
A `build()` that raises, or an attach that exceeds `BEHEROUTER_ATTACH_TIMEOUT_S` (default
30 s), leaves that surface answering RFC 9457 `503` while the gateway retries it (5 s,
doubling to 300 s) and swaps it in on the first success. `/healthz` stays HTTP 200 and
reports `"status": "degraded"` with the surface under `failed`; the error is only in the
container logs, so read them after any registry change. A configuration fault found only at
attach (a `UsageError`) is **not** retried: the surface also appears under
`needs_config_change`. What `registry-lint` can see — an unknown plugin, a bad config value,
an unset `${VAR}` — **still refuses boot**.

⚠️ **A probe matching `"status":"ok"` fails on a degraded gateway.** That is intended:
`helm test` does it, and so should an external monitor (a blackbox probe matching that
string). Check that yours alerts on it rather than ignoring the field.

⚠️ **A same-host backend needs a shared container network.** A rootless bridged container
**cannot reach a port published on its own host** — so co-locating a backend makes it
*harder* to reach, not easier. Loopback, the host-gateway alias, the LAN address and the
public vhost all refuse, while remote hosts answer 200. Join both containers to a shared
network and address the backend by its network alias. Expect older documentation to claim
the opposite.

⚠️ **A Django backend must be addressed by a network alias without underscores.** Django's
host validation permits only `[a-z0-9.-]` plus an optional port and rejects a bad `Host`
with a bare **400 before `ALLOWED_HOSTS` is consulted**. Since podman-compose names
containers with underscores, the obvious value is the broken one.

⚠️ **Beware a frontend with a catch-all route.** Probing an API path against a Next.js
frontend can return 200 where the real API returns 401 — so the surface attaches cleanly and
then returns HTML where the SDK expects JSON. Point at the API, not the web port.

⚠️ **After any rootless-podman major upgrade, run `podman ps`.** A 5.x → 6.x jump can break
rootless podman unless the storage configuration pins the runroot, and the failure is
**latent**: running containers keep running and every health check stays green until the
unit is next cycled, at which point it cannot come back. "The service is up" proves nothing
here.

⚠️ **Build tooling must actually be present.** A slim Python base image has no `git`; an
image that clones its own source at build time must install it.

# Hot reload, kill switch and the admin endpoint

**Date:** 2026-10-09
**Status:** Approved design, not yet built
**Answers:** client asks A4 and A11 (`docs/requests/BEHEROUTER-WHAT-WE-STILL-NEED.md`)
**Builds on:** `2026-10-08-call-pipeline-observability-design.md` (the `CallPipeline`, the
reason vocabulary, the reserved reasons) and `2026-10-08-call-gates-design.md` (the stage
seams, the `RateLimiter`)

## 1. Goal

| Ask | What lands | Refusal reason |
|---|---|---|
| A4 | a registry reload that re-attaches only the surfaces that changed, triggered by SIGHUP, `POST /admin/reload` or a file watch; `${file:/path}` registry values so a rotated secret reaches a running gateway | — |
| A11 | a kill switch (stop every surface, quarantine one, block one caller `sub`) held in a state file and driven by an authenticated admin API, effective on the next call, no restart | `surface_disabled` / `caller_blocked` |

All of it is opt-in. With no admin credential, no kill-switch path and no watch interval, the
gateway behaves as today, except that the three reserved surface names (§5.4) are now refused.

Out of scope:
- A3: sessions across replicas.
- Coordinating a reload across pods. Each pod reloads itself.
- Making gateway-wide environment reloadable (§2.6).
- The two carried executor items: the audit `status` is always `null`, and an upstream 5xx is
  classified as `backend_rejected`.
- The parked sub-project 2 minors.

## 2. Hot reload (A4)

### 2.1 The surface table

Today each surface is a Starlette `Mount`, fixed when the app is built. A failed one is a
`_PendingSurface` whose `.app` a retry task fills in later. That indirection becomes the rule
for every surface:

- **`SurfaceSlot`** generalizes `_PendingSurface`. It holds the current ASGI app, or none
  (the 503 problem body unchanged), the surface's fingerprint (§2.3), its supervisor task and
  its `config_fault` flag.
- **`SurfaceRouter`** is one ASGI app mounted after the explicit routes. It dispatches
  `/<name>/…` to `slots[name]` and answers RFC 9457 404 for an unknown name. The table is a
  dict that a reload swaps whole, so a request sees either the old table or the new one.
- **A supervisor task per slot** owns its sub-app's lifespan, exactly as `_retry_attach` does
  today: the lifespan is entered and exited in the same task, never pushed onto the parent's
  exit stack.
  - Boot-attached surfaces get one too, so boot, retry and reload all reduce to "start a
    supervisor that puts an app in a slot".
  - The supervisor calls `_track_sessions` for every app it installs.
  - It closes its backend when cancelled, after the lifespan has exited.

The published `tools` array still freezes per app instance. An unchanged surface is never
rebuilt, so its hosts' prompt caches survive a reload.

### 2.2 Triggers

All three call `Reloader.request(trigger)`:

| Trigger | How | Notes |
|---|---|---|
| `sighup` | `loop.add_signal_handler(SIGHUP, …)` in the lifespan | fire and forget; the outcome is logged |
| `admin` | `POST /admin/reload` (§4) | waits for the reload and returns its result |
| `watch` | `BEHEROUTER_REGISTRY_WATCH_S` > 0 starts a poll task; unset or 0 = off | stats `registry.toml` and every `${file:…}` path the registry references, keyed on `(mtime_ns, size, ino)` with symlinks followed, so the atomic symlink swap of a Kubernetes ConfigMap or Secret volume is seen |

One `asyncio.Lock` serializes reloads. A request that arrives while a reload runs sets a
"pending" flag. When the reload finishes, **one** follow-up reload runs for every request
that arrived meanwhile. An `admin` caller that arrived mid-reload waits for, and receives,
the result of that follow-up.

### 2.3 The reload

1. **Load and lint, all or nothing.** The steps are `load_registry` → `preflight` (now also
   resolving `${file:…}`) → the boot-time checks in `build_gateway_app`, factored into one
   `check_registry(registry)` that boot and reload share:
   - shared auth mode with a per-user surface;
   - a role gate without `OIDC_ROLES_CLAIM`.

   The admin checks of §4.2 read only process environment, which a reload cannot change
   (§2.6), so they run once, at boot.

   On any failure the **whole old registry stays in force**. The failure is logged at ERROR
   with its type and message, and §6.2's `last_reload` reports it. An `admin` trigger gets a
   422 problem.
2. **Diff by fingerprint.** The fingerprint is
   `sha256(canonical JSON of the entry + the resolved value of every placeholder)`. Only the
   digest is kept, never a value. Each name falls into one of four sets: `added`, `removed`,
   `changed` or `unchanged`. A rotated `${file:}` secret changes only its own surface's
   fingerprint.
3. **Attach `added` ∪ `changed` concurrently**, through the same `_attach_one` and
   `_finish_attach` that boot uses, under `BEHEROUTER_ATTACH_TIMEOUT_S`:

   | Case | Result |
   |---|---|
   | added, attached | new slot, live |
   | added, failed | new slot, pending, with a retry supervisor, or `needs_config_change` for a config fault. As at boot. |
   | changed, attached | the slot's app is swapped. The old app drains (§2.4) and closes. |
   | changed, failed | **the old app keeps serving.** The name goes into `reload_failed` (§6.2), and a retry supervisor for the *new* entry swaps it in when it attaches. A config fault stops the retry, and the old app keeps serving. A reload never turns a working surface into a 503. |
   | changed, previously pending | its old retry supervisor is cancelled, and the new entry's attach or retry replaces it |
   | unchanged | not touched: same app, same sessions, same limiter |

4. **Removed surfaces:** the slot is dropped from the table, so the path answers 404. Its
   gauges are `remove()`d (§6.1), and its old app drains and closes.
5. **The result** is `{trigger, outcome, added, changed, removed, unchanged, failed}`, with
   names only. `outcome` is:
   - `ok`: everything attached;
   - `partial`: at least one added or changed surface failed to attach;
   - `failed`: lint failed (step 1).

   It is logged at INFO (WARNING for `partial`, ERROR for `failed`), counted (§6.1), and
   returned to an `admin` caller.

### 2.4 Drain

A swapped-out or removed app stops receiving **new** requests at once. Requests already in
it keep going for `BEHEROUTER_RELOAD_DRAIN_S` (default 30, ≥ 0). Then its supervisor is
cancelled and its backend closed. A streamable-HTTP GET stream never ends on its own, which
is why there is a deadline at all. A changed surface's MCP sessions end at the swap, because
they live in the old app's session manager. Clients re-initialize against the new app, whose
`tools` array freezes anew. That is correct, because the surface did change.

### 2.5 Rate limiter carry-over

The handoff left this open. The decision: **carry the limiter over.** A `changed` surface
whose `[surface.rate_limit]` table equals the old one reuses the old `RateLimiter` object.
`gates_from_entry` gains an optional `previous: Gates | None` and reuses its limiter when the
limit is equal. Otherwise every reload, a token rotation included, would hand every caller a
full burst. A changed limit starts with full buckets, and this is documented in
`docs/DEPLOYMENT.md` § Rate limits.

### 2.6 What a reload cannot change

Gateway-wide settings are process environment, read once:
- `BEHEROUTER_AUTH_MODE`, the OIDC and JWKS settings, the shared client token;
- the admin variables (§4.2), the audit switches, the log format, the timeouts;
- `BEHEROUTER_KILLSWITCH_PATH` and the watch and drain settings.

They stay restart-only. `docs/DEPLOYMENT.md` says so, so nobody expects a SIGHUP to rotate
the shared client token. `${VAR}` placeholders in the registry re-resolve on reload but read
an environment that never changes, so `${file:…}` is the rotation path (§3).

## 3. `${file:/path}` placeholders

`envexpand.PLACEHOLDER` gains a second form: a whole value that is exactly
`${file:/absolute/path}`.

- **Read** at `preflight` (boot, `registry-lint`, reload) and at attach, never per call.
- **Exactly one trailing newline is stripped.** Kubernetes Secrets and `echo > file` add one.
- **A relative path, a missing, unreadable or empty file** is a `UsageError` worded like the
  unset-`${VAR}` one. It names the path, never the content.
- **Never logged.** The fingerprint (§2.3) hashes the value. `registry-lint` reports
  existence and non-emptiness only.
- A bind-mounted secret must be readable by UID 1000. This is already the rule for every
  file the gateway reads.

## 4. Admin API

### 4.1 Routes

The admin routes are explicit Starlette routes ahead of the `SurfaceRouter`, like `/healthz`
and `/metrics`.

| Route | Does | Exists when |
|---|---|---|
| `POST /admin/reload` | §2.3; 200 with the result, 422 problem when lint failed | admin auth is configured |
| `GET /admin/killswitch` | the state (§5.1) as JSON | admin auth **and** `BEHEROUTER_KILLSWITCH_PATH` |
| `PUT` / `DELETE /admin/killswitch/all` | stop / resume every surface; `PUT` body `{"reason"}` | same |
| `PUT` / `DELETE /admin/killswitch/surfaces/{name}` | quarantine / release one surface; `PUT` body `{"reason"}` | same |
| `POST /admin/killswitch/subjects/block` | body `{"sub", "reason"}` | same |
| `POST /admin/killswitch/subjects/unblock` | body `{"sub"}` | same |

- A `sub` travels **in the body, never in the path**, so it never lands in a proxy's access
  log.
- `reason` is optional free text, at most 200 characters. It is stored in the file and never
  logged.
- An unknown surface name is accepted, with `"warning": "no such surface"` in the response,
  so an operator can quarantine a surface before it is added.
- Every write answers with the new state. A write when the state file cannot be written
  (read-only mount, EROFS or EACCES) answers **409**, saying the file is read-only and must be
  edited where it is managed.
- Bad JSON or a wrong shape answers 400. Every error body is RFC 9457.

### 4.2 Authentication

`Authorization: Bearer <credential>`. Either of these passes:

- **`BEHEROUTER_ADMIN_TOKEN`**, compared with `hmac.compare_digest`. Boot is refused if it is
  equal to the shared client token: one secret must not do two jobs.
- **A JWT verified by the gateway's existing verifier** (JWKS, issuer, audience) whose roles
  claim contains **`BEHEROUTER_ADMIN_ROLE`**. Setting it requires `BEHEROUTER_AUTH_MODE`
  `oidc` or `both`, and `BEHEROUTER_OIDC_ROLES_CLAIM`. Otherwise boot is refused, with the same
  wording as the surface role-gate rule.

| Situation | Answer |
|---|---|
| neither variable set | **no `/admin` route exists** (404), so an upgrade adds no network surface |
| no credential, or an invalid one | 401 |
| a valid JWT without the role | 403 |

Rejections count in `beherouter_auth_rejections_total` with `surface=""`, the existing rule
for a path that names no surface. The routes listen on the gateway's own port and bind. Behind
the reference reverse proxy, `/admin` stays unreachable until an operator adds a clause for
it, because the proxy is default-deny. `docs/DEPLOYMENT.md` says to allow it from the admin
network only.

### 4.3 Audit

Every admin request, refusals included, writes one `beherouter.audit` line:

```json
{"event": "admin", "action": "block_subject", "target_kind": "subject",
 "target": "sha256:3f2a9c1e", "actor": "<sub or <admin-token>>", "outcome": "ok"}
```

- `target` is the surface name, `*` for `all`, `-` for `reload`, and for a subject the first
  8 hex characters of SHA-256 of the `sub`. That is enough to match a block to its unblock,
  and the log never holds the value.
- No body, no `reason` text, no credential.
- `BEHEROUTER_AUDIT=off` silences these lines too.

## 5. Kill switch (A11)

### 5.1 The state file

`BEHEROUTER_KILLSWITCH_PATH` turns the kill switch on. Unset = off, and the kill-switch routes
do not exist.

```json
{"all": {"reason": "incident 42", "at": "2026-10-09T10:00:00+00:00", "by": "<actor>"},
 "surfaces": {"dwh": {"reason": "quarantined", "at": "…", "by": "…"}},
 "subjects": {"user-123": {"reason": "offboarded", "at": "…", "by": "…"}}}
```

- `all` is either an object or absent. `at` and `by` are written by the API and optional
  when a human edits the file.
- `by` is the actor (§4.3). It is an operator's identity in a file only operators read.
- **Missing file:** empty state. The first API write creates it.
- **Write:** read-modify-write under a process lock, then a temp file in the same directory,
  `fsync`, `rename`, mode 0600.
  - Two admins writing through **different pods** to one shared RWX file can race, and the
    last writer wins. This is documented, not solved.
  - Each write re-reads the file first, so a human edit made between two API writes is kept.

### 5.2 Reading it

`KillSwitch.state()` stats the path on every call and re-parses only when
`(mtime_ns, size, ino)` changes. This is the identity map's pattern (`identity.py`, the
stat-cached map). A ConfigMap, a Secret, an RWX volume or a hand edit takes effect on the next
call, with no reload.

**A malformed file** (bad JSON or a wrong shape):
- The **last-good state stays in force.** An ERROR is logged once per distinct stamp, and
  `/healthz` shows `killswitch: "stale"`. Failing open on a typo would silently lift every
  block, and failing closed would turn a typo into a total outage.
- **At boot** there is no last-good state, so a malformed file **refuses boot**.
- `registry-lint` validates the file too when the variable is set.

### 5.3 Enforcement

`KillSwitchStage` is a surface-wide `Stage` in `CallPipeline.stages`, as sub-project 2
ruled. It runs before `work`, so it covers pinned tools and the meta-tools alike. It is
added only when the kill switch is configured, so with it off the call path is unchanged.
It checks, in this order:

1. `all` is set → `surface_disabled`, context `{"scope": "all"}`;
2. the surface is listed → `surface_disabled`, context `{"scope": "surface"}`;
3. the caller's verified `sub` is listed → `caller_blocked`.

- **Order matters:** a blocked caller on a stopped surface learns only that the surface is
  stopped.
- The refusal is a tool-level `isError` result, not an HTTP 401 or 403. LibreChat reads a 401
  as "OAuth required", and an agent can read a tool error. `initialize` and `tools/list` still
  answer, so a quarantined surface reads as switched off, not broken.
- The refusal text names the category ("this surface is stopped by an administrator", "your
  access to this gateway is suspended"). It never names the stored `reason` or the caller's
  `sub`.
- A shared-token caller has no `sub`. It is stopped by surface or `all` only.
- **The 900 s JWT gap is closed:** a blocked `sub` is refused on its next call, whatever its
  token's expiry.
- `health --deep` still calls `backend.executor.run` directly and is not stopped by the kill
  switch. This is intentional, matches its treatment of the gates, and is documented.

### 5.4 Reserved names

`validate_entry`, and therefore `registry-lint`, `preflight` and reload, refuses a surface
named `admin`, `healthz` or `metrics`. Today a surface named `healthz` is silently shadowed by
the explicit route.

## 6. Signals and vocabulary

### 6.1 Metrics

No new label carries a caller.

| Series | Type | Labels |
|---|---|---|
| `beherouter_reloads_total` | counter | `trigger` (`sighup`/`admin`/`watch`), `outcome` (`ok`/`partial`/`failed`) |
| `beherouter_reload_last_success_timestamp_seconds` | gauge | — |
| `beherouter_surface_disabled` | gauge 0/1 | `surface` (1 for every surface while `all` is set) |
| `beherouter_blocked_subjects` | gauge | — (a count) |

A removed surface's gauges (`surface_up`, `active_sessions`, `surface_disabled`) are
`remove()`d. Its counters and histograms stay, because `rate()` over history still means
something, and a counter that vanishes mid-series is worse than one that stops rising.

### 6.2 `/healthz`

Every new key is present only when it is non-empty, so a clean gateway still answers exactly
`{"status", "surfaces"}`:

| Key | Meaning |
|---|---|
| `reload_failed` | surfaces still serving their pre-reload app because the new entry failed to attach |
| `last_reload` | `{"status": "failed", "at": …}`, present only while the last reload failed at lint |
| `disabled` | quarantined surface names, or `["*"]` while `all` is set |
| `killswitch` | `"stale"` while a malformed file is ignored |

The kill switch does **not** make `status` degraded. `degraded` means an attach failed, and a
deliberate stop is not a fault. Alert on `disabled` instead. `reload_failed` does not degrade
`status` either, because the surface is serving.

### 6.3 Errors

- `errors.REASONS` gains `surface_disabled` and `caller_blocked`, both kind `refused`,
  non-transient.
- `outcomes.CONTEXT_KEYS` gains `scope`.
- Both reach `beherouter_tool_calls_total{outcome="refused"}` and the call's audit line
  through the existing `classify` path.

## 7. Chart

Chart `version` 0.1.7 is unreleased, so this needs no further bump.

- `admin.token` → the chart Secret's `BEHEROUTER_ADMIN_TOKEN` (with `secret.create=false`, it
  arrives through `extraEnv`); `admin.role` → `BEHEROUTER_ADMIN_ROLE`.
- `killswitch.enabled` with `killswitch.existingClaim` (required; RWX when
  `replicaCount` > 1), mounted at `killswitch.mountPath` →
  `BEHEROUTER_KILLSWITCH_PATH=<mountPath>/killswitch.json`. Off by default. When it is on, the
  pod gets `fsGroup: 1000` unless `podSecurityContext` sets one, so UID 1000 can write the
  claim.
- `secretFiles.enabled` with `secretFiles.secretName`: an existing Secret mounted **as a
  directory** (never `subPath`, which does not update) at `secretFiles.mountPath`
  (`/run/secrets/beherouter`), for `${file:…}` values. This is what makes rotation work
  without a restart on Kubernetes.
- `hotReload.enabled: false` (top level, because `registry` is a plain string). When it is
  true:
  - the `checksum/configmap-registry` annotation is dropped (`deployment.yaml:34`), otherwise
    every ConfigMap edit rolls the pods anyway;
  - `BEHEROUTER_REGISTRY_WATCH_S` is set to `hotReload.watchSeconds` (default 30).
- `/admin` gets no ingress path by default.
- `helm test` and the hook Jobs are unchanged.

## 8. Testing

Fake plugins against `build_gateway_app`:

- **Reload:** every row of the §2.3 table.
  - Sessions on an unchanged surface survive a reload. One session stays open across a
    reload that changes a sibling.
  - A lint failure keeps the old registry and records `last_reload`.
  - A changed surface whose attach fails keeps serving its old app and is listed under
    `reload_failed`, then swaps in when its retry succeeds.
  - Removal: 404 afterwards, gauges removed, backend closed after the drain.
  - Coalescing: three requests during one reload give exactly one follow-up.
  - The drain deadline, and limiter carry-over: equal limit reused, changed limit fresh.
  - The SIGHUP handler, and the watch poll seeing a registry change and a `${file:}` change.
- **`${file:}`:** set; trailing newline stripped (exactly one); missing, unreadable, empty and
  relative paths refused by name; `registry-lint` never prints the content.
- **Kill switch:**
  - the stat cache;
  - a malformed file keeps last-good and sets `killswitch: "stale"`; malformed at boot
    refuses boot;
  - atomic write at mode 0600; a read-only directory answers 409;
  - a human edit is kept across an API write;
  - stage order, and the meta-tools refused too;
  - shared-token callers.
- **Admin auth:**
  - token, role, 401 and 403;
  - no route without configuration (404);
  - admin token equal to the shared token refuses boot;
  - the role without OIDC refuses boot.
- **Never echo:** a `caplog` plus audit-sink assertion that no blocked `sub` value and no
  `${file:}` content appears in any log record, audit line, `/healthz` or `/metrics` body.
- **Reserved names** at lint.
- **Gates:** ruff, mypy, `pytest --cov` with the 90 % floor unchanged or raised.

## 9. Docs

Written alongside the code:
- **`docs/DEPLOYMENT.md`:** Hot reload; Kill switch; Admin API; `${file:}` secrets and
  rotation; what a reload cannot change; § Rate limits on carry-over.
- **`docs/IDENTITY.md`:** blocking a `sub` and the closed 900 s gap.
- **`AGENTS.md`:** operator signals, the routes, the reserved names.
- **`README.md`**, **`CHANGELOG.md` § Unreleased**, and the chart's values comments.

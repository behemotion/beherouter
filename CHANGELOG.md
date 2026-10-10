# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/):
one section per release, newest first, and the section for the version being
released is the body of its GitHub Release (`release.yml` extracts it, and the
auto-generated contributor appendix is appended below it). Releases before
0.2.0 were not tagged; their history is the git log.

## [Unreleased]

### Added

- **Per-tool role gates.** `[surface.authz.tools.<name>] require_roles` (all roles,
  on top of the surface gate). A shared-token caller keeps the ungated tools; a
  caller failing a gate does not see the tool in `tools/list` or `search_tools`
  (`hide_tools` is now valid beside `tools` alone), and `describe_tool` /
  `run_tool` answer `missing_role` (`unauthenticated` for a shared-token caller). See `docs/IDENTITY.md` §6c.
- **`[surface.rate_limit]`** (`calls`, `per_s`, `burst`): a per-caller token
  bucket over calls that reach the backend. New reason `rate_limited` with
  `retry_after_s` and `limit`. In memory, per replica. See `docs/DEPLOYMENT.md`
  § Rate limits.
- **`confirm_mutating` / `confirm_exempt`**: a human confirms mutating (or
  unknown-`mutating`) calls through MCP elicitation. New reason
  `confirmation_required` with `confirmation` `unsupported` | `declined` |
  `timeout`; `describe_tool` reports `requires_confirmation`. Needs a stateful
  session and an elicitation-capable client.
- **Generic plugins: `mcp-http`, `mcp-stdio` and `beheaxi-cli`.** A backend with
  no curated plugin attaches by URL, by command, or as any beheaxi CLI.
  `beheaxi-cli` is the first production plugin on the `cli` backing. All three
  require `probe` in the registry entry, and the two MCP ones also require
  `pinned`, so an unprobed surface still can't be configured. `mcp-http` honours
  identity modes `bearer`, `claims`, `client` and `exchange`; `beheaxi-cli`
  honours `claims`; `mcp-stdio` honours none. See `docs/PLUGINS.md` § Generic
  plugins.
- **Optional credentials.** `EnvVar(required=False)` declares a credential an
  entry may omit. Undeclared names are still refused. `plugin-config` emits an
  optional credential commented out, so the generated block passes
  `registry-lint` and nothing un-vaulted reaches the env file.
- `beherouter plugins` reports `generic` for each plugin: true when the entry
  must supply a key the plugin has no tested default for.
- **Identity mode `exchange` (OAuth 2.0 Token Exchange, RFC 8693).** The gateway
  trades the caller's verified token at the IdP for one addressed to the
  backend and forwards that, as a header. Declared by `mcp-http`, `openapi`,
  `office-mcp` and `plane-http`. Issued tokens are cached per surface and caller
  digest until 30 s before `expires_in` (LRU, 1024 entries), and concurrent
  calls for one caller share one exchange. Nothing falls back: a failed
  exchange never sends the call. The `client_secret` `${VAR}` is resolved per
  exchange, and `registry-lint` warns when it is unset. See `docs/IDENTITY.md`
  and `docs/superpowers/specs/2026-10-07-token-exchange-design.md`.
- **`beherouter calendar-consent <surface> --subject <value>`**: one user's
  Google or Microsoft consent for a `lookup` calendar surface, as a PKCE
  loopback flow on `127.0.0.1`, written into the identity map atomically with
  mode 0600. `--revoke` removes the entry. Replaces the hand-run `nc`/`curl`
  procedure per user; see `docs/CALENDAR-BOOTSTRAP.md`.
- **`beherouter catalog-import <file|URL|name> <surface> [--registry URL]`**
  turns an MCP Registry `server.json` (schema 2025-12-11) into `mcp-http` or
  `mcp-stdio` registry fragments with exact version pins. `pinned` and `probe`
  it cannot know are emitted as TODOs that fail `registry-lint`, unless the
  document carries an `io.beherouter/plugin` block under `_meta`. Credentials
  beyond the one `api_key` are warned about, never emitted.
- **`beherouter catalog-export <plugin>`** emits a curated plugin as a
  `server.json` with its pins and probe under `_meta` (`--name`,
  `--server-version`, `--url`, `--package`).
- **Maturity tiers**: `declared` < `probed` < `catalogued` < `verified` <
  `per-user`. A plugin states its tier and evidence (`PluginSpec(maturity=,
  evidence=)`); `beherouter.testing.plugin_conformance` proves the claim in a
  test suite, `beherouter plugins` shows it, and `registry-lint` warns on a
  surface using a `declared` plugin. An out-of-tree plugin whose evidence does
  not ship in its own distribution is shown as `probed` at most. See
  `docs/PLUGINS.md` § Maturity.
- **`beherouter health --deep --textfile PATH`** writes the sweep as a
  node_exporter textfile-collector file, atomically and with no identity value
  in it; with `--bearer-file` it carries the per-user verdict too.
  `contrib/health-textfile/` ships a systemd timer, a Kubernetes CronJob and
  alert rules, and the chart (0.1.7) has an opt-in `healthCronJob`.
- **`beherouter --version`**, read from the installed package metadata.
- **Hot reload (client ask A4).** `kill -HUP`, `POST /admin/reload` or
  `BEHEROUTER_REGISTRY_WATCH_S` re-reads the registry and re-attaches only the
  surfaces whose entry changed; unchanged surfaces keep their sessions and rate
  limiter. A registry that fails lint changes nothing (`/healthz` `last_reload`).
  A changed surface that fails to attach or start keeps serving its old app
  (`/healthz` `reload_failed`). Swapped-out apps drain for
  `BEHEROUTER_RELOAD_DRAIN_S` (default 30). Reloads coalesce. Gateway-wide
  environment stays restart-only. See `docs/DEPLOYMENT.md` § Hot reload.
- **`${file:/absolute/path}` registry values**: the file's content (one trailing
  newline stripped), read at lint, attach and reload, so a rotated secret reaches
  a running gateway. Missing, unreadable, empty and relative paths refuse by
  path, never by content.
- **Kill switch (client ask A11).** `BEHEROUTER_KILLSWITCH_PATH` names a JSON state
  file that stops every surface, quarantines one, or blocks one caller `sub`,
  effective on the next call (a JWT's remaining lifetime no longer matters).
  New reasons `surface_disabled` (context `scope`) and `caller_blocked`, both
  `refused`. A malformed file keeps the last good state (`/healthz`
  `killswitch: "stale"`), and refuses boot when there is none. `/healthz` `disabled`.
  See `docs/DEPLOYMENT.md` § Kill switch and `docs/IDENTITY.md`.
- **Admin API**, off unless `BEHEROUTER_ADMIN_TOKEN` or `BEHEROUTER_ADMIN_ROLE` is
  set: `POST /admin/reload` and the `/admin/killswitch` routes. Boot refuses an
  admin token equal to the gateway token, and a role without `oidc`/`both` and a
  roles claim. Every request writes one `event: admin` audit line (`target` for a
  subject is `sha256:<8 hex>`, `actor` is the admin's own `sub`, `<admin-token>`, `<unknown-subject>` (an admin JWT
  without a `sub`) or `<refused>` (a refused request);
  never a body or reason).
- **Metrics:** `beherouter_reloads_total{trigger,outcome}`,
  `beherouter_reload_last_success_timestamp_seconds`,
  `beherouter_surface_disabled{surface}`, `beherouter_blocked_subjects`. A removed
  surface's gauges are removed. `beherouter_blocked_subjects` and
  `beherouter_reload_last_success_timestamp_seconds` are exported on **every**
  gateway, as `0` when no kill switch is configured or no reload has succeeded
  yet; alert on them only where the feature is enabled.
- **Chart:** `admin.token` (fails the render together with `secret.create=false`:
  supply it through `extraEnv`), `admin.role`, `killswitch.{enabled,existingClaim,mountPath}`,
  `secretFiles.{enabled,secretName,mountPath}` and `hotReload.{enabled,watchSeconds}`
  (which drops the registry checksum annotation so a ConfigMap edit does not roll
  the pods). New env vars: `BEHEROUTER_REGISTRY_WATCH_S`, `BEHEROUTER_RELOAD_DRAIN_S`,
  `BEHEROUTER_ADMIN_TOKEN`, `BEHEROUTER_ADMIN_ROLE`, `BEHEROUTER_KILLSWITCH_PATH`.
- **`scripts/deploy.sh`**: a single-host podman deploy that lints the registry
  inside the new image, snapshots the registry per deploy, verifies `/healthz`
  (and optionally `health --deep`) and rolls back to the previous container
  and registry on failure. A repeatable `--volume SRC:DST[:OPTS]` reaches both the
  lint run and the gateway, for `${file:}` secrets and a writable kill-switch
  directory; a registry change still means re-running it (the snapshot is what
  makes rollback restore the old config). See `docs/DEPLOYMENT.md`.
- `/healthz` gains two optional keys: `needs_config_change` (failed surfaces
  the gateway has stopped retrying) and `pinned_missing` (per surface, pinned
  tools the backend no longer serves).
- `beherouter plugins` lists entry points that failed to load under `failed`;
  `registry-lint` warns about them, and an `unknown plugin` error names them.
- Search eval sets for `sonarqube` (1.27.0.4335, 18 tools) and `office-mcp`
  (0.1.0, 4 tools), with search aliases: top-3 hits go from 23/33 to 33/33 and
  from 9/18 to 18/18. `gcal`/`m365` gain a recorded catalogue
  (`tests/fixtures/catalogues/calendar-0.2.5.json`).
- **Audit line.** Every tool call writes one JSON line on `beherouter.audit` (stdout):
  surface, tool, inner tool, caller `sub` plus the claims named in
  `BEHEROUTER_AUDIT_CLAIMS`, outcome, reason, status, latency, `call_id`. Never the
  arguments. `BEHEROUTER_AUDIT=off` disables it.
- **Metrics.** `beherouter_tool_calls_total`, `beherouter_tool_call_duration_seconds`,
  `beherouter_active_sessions` and `beherouter_surface_up` on `/metrics`.
- **`call_timeout_s`** (registry entry) and `BEHEROUTER_CALL_TIMEOUT_S`: a bound on the
  backend call, unset = no limit. Expiry ends the call with reason `timeout`.
- **Machine-readable errors.** A failed call carries
  `_meta["io.beherouter/error"] = {type, code, reason, context}` with a documented
  `reason` enum. See `docs/DEPLOYMENT.md` § Logs, audit and metrics.
- **JSON logs.** `BEHEROUTER_LOG_FORMAT=json` writes one object per line with a local-offset
  timestamp; the default `text` format no longer prints multi-line tracebacks for expected
  refusals.
- **`stateless = true`** on a registry entry serves the surface with no MCP
  session (FastMCP stateless streamable-HTTP): a rollout or reload drops
  nothing and replicas need no client affinity. Refused beside
  `confirm_mutating`; the surface has no `beherouter_active_sessions` series.
  FastMCP's own `FASTMCP_STATELESS_HTTP` environment variable no longer has any effect:
  the gateway always passes the registry key's value (default stateful).
  Verified against LibreChat 0.8.7. See `docs/DEPLOYMENT.md` § Stateless sessions.
- **Chart: more plugin sources.** `plugins.indexes[]` (`name`, `url`,
  `credentialsSecret`) adds named uv indexes searched before `indexUrl`/PyPI,
  each with its own credential Secret (`UV_INDEX_<NAME>_USERNAME`/`_PASSWORD`);
  uv's first-index strategy is kept. `plugins.local` (`configMap`, `wheels`)
  installs wheels from a ConfigMap through the same installer and safeguards:
  listing each wheel's filename makes an upgrade roll the pods, and the
  ConfigMap is mounted `optional`, so a missing one fails the init container
  with a log naming the ConfigMap and file instead of holding the pod in
  ContainerCreating. Invalid names, duplicates, the reserved `plugins` and
  non-wheel filenames fail the render. `plugininstall` refuses a missing local
  wheel before running uv. See `docs/DEPLOYMENT.md` § Private CA, out-of-tree
  plugins.

### Changed

- **Behaviour change: `admin`, `healthz` and `metrics` are reserved surface
  names.** `registry-lint`, boot and reload refuse them; a surface named
  `healthz` used to be silently shadowed by the route.
- **Behaviour change: an unknown path answers an RFC 9457 `404` problem body**
  (it was Starlette's plain-text 404).
- **A failed call returns an error result instead of raising**: `isError: true` with
  `_meta`, text unchanged apart from dropping FastMCP's `Error calling tool '<name>': `
  prefix. Backend refusals log one WARNING line instead of an ERROR traceback.
- **`beherouter_auth_rejections_total` gains a `surface` label** (`""` when the path names
  no configured surface); its values now render as floats.
- **The audit line is on by default**: a new JSON stream on stdout, beside the
  log on stderr. Set `BEHEROUTER_AUDIT=off` to disable it; any other value but
  `on` refuses boot.
- **One `beherouter.calls` line per call replaces the INFO `identity applied …`
  line.** It carries the same names-only identity fields (`subject`, `mode`,
  `keys`), but on every call of every surface, so `subject` now appears in the
  log for each call rather than only on identity-enabled surfaces.
  A successful call's line is DEBUG while the audit is on, since the audit
  line already records it; with `BEHEROUTER_AUDIT=off` it stays at INFO.
- **`beherouter serve` takes over logging.** One plain handler (text or JSON)
  replaces FastMCP's Rich and uvicorn's own formats. Third-party loggers stay
  quiet below WARNING (`httpx`, `httpcore` and `mcp`, whose INFO lines carry
  request URLs and session ids), so INFO output is beherouter's own.
- **`/metrics` Content-Type is now prometheus-client's**:
  `text/plain; version=1.0.0; charset=utf-8`.
- New dependency: `prometheus-client`.
- **`plugin-config` and `catalog-import` emit plain env lines** (`VAR=`, empty)
  instead of an ansible `{{ vault_… }}` template, so the fragment is correct for
  a `.env` file, a Kubernetes Secret or any vault render. The value is left
  empty on purpose: an unfilled `${VAR}` refuses boot by name rather than
  booting into 401s.

- **Surfaces attach concurrently**, and `health --deep` probes them
  concurrently. Registry order is kept in the output, and when several fail,
  the first failure in registry order is the one raised.
- **A pinned tool missing at attach logs a WARNING and appears under
  `/healthz` `pinned_missing`**; the surface status is unchanged.
  `health --deep` still fails it (`catalogue: "pinned_missing"`).
- Probe resolution and the missing-pins check have one implementation each
  (`plugins.resolve_probe`, `gateway.missing_pins`), shared by attach, health
  and lint.
- `attach` and `detach` edit `registry.toml` in place (new dependency
  `tomlkit`), keeping the operator's comments and layout.
- `attach --config` coerces values (integers, floats, `true`/`false`); a list
  is written by repeating the key.
- Backends are closed when they are no longer served: on shutdown (surfaces
  attached by retry included), when dropped after build, when `build_surfaces`
  raises, and after each `health --deep` and `context-cost` probe.
- `registry-lint` also warns when a `cli` plugin's `cmd` is not on `PATH`, as it
  already did for `stdio`.
- Chart 0.1.7: the registry-lint hook and test pods no longer carry the
  gateway's selector labels, so the Service, PodDisruptionBudget and
  NetworkPolicy cannot count them as gateway replicas.
- Images build from the committed `uv.lock` (`uv sync --frozen`), with `uv`
  pinned by version and digest, as UID 1000. `.dockerignore` mirrors
  `.containerignore`, and `.env` files are excluded from the build context.
- CI runs ruff (an extended rule set), mypy over `src`, the test suite with a
  90 % branch-coverage floor, `pip-audit`, hadolint, shellcheck, the
  version-consistency gates and the e2e stack; every action is pinned to a
  commit SHA and kept current by Dependabot, and workflows run with read-only
  default permissions.

### Fixed

- **A backend's extra text blocks reach the model.** Once a backend returned a
  structured value, every content block was dropped, so a warning appended to a
  structured reply (a Plane middleware's "NOT ASSIGNED: …") never left the
  gateway and the model reported a partly failed call as done. Text blocks that
  are not the structured value serialized now travel beside it as `notes` (a
  string array, present only when non-empty, declared in the published output
  schema), for pinned tools and `run_tool`, on `http`, `stdio` and `inproc`
  backings. Non-text blocks are still dropped. See `docs/PLUGINS.md` § A
  backend's extra text blocks.
- **A stdio backend's subprocess is stopped when the backend is dropped.**
  `keep_alive` kept the child running after a reload changed or removed its
  surface, after a failed or cancelled attach, and after `health --deep`.
- **The audit line's `status` is filled in.** It was always `null`; it now
  carries the upstream HTTP status when the backend gave one. An MCP backend
  answering HTTP 4xx is `backend_rejected` (it was `backend_unavailable`); a
  5xx stays `backend_unavailable`. An MCP `isError` result has no status and
  stays `backend_rejected`.
- **An app that dies while serving is no longer reported live.** Its surface
  answers `503`, appears under `/healthz` `failed`, and is retried.
- A retry cancelled by a reload's removal can no longer re-create the removed
  surface's `beherouter_active_sessions` series.
- `stateless = false` written out no longer counts as a change at reload.
- A reload that fails after every requester has gone is logged, and asyncio no
  longer reports an unretrieved exception for it.
- Kill switch: a malformed file is not re-parsed on every call while it stays
  unchanged; an unreadable file's error names its type cleanly; the state file
  is written off the event loop; an admin write that would leave an invalid
  file answers `400`; the state returned by the admin API is a copy.
- **A configuration fault at attach is no longer retried.** A `UsageError`
  (for example a bad `[surface.identity]`) used to be retried forever; the
  surface's `503` now says it will not be retried and carries no
  `Retry-After`, and `/healthz` lists it under `needs_config_change`.
- `beherouter --version` and the manifest reported `0.1.0`; they report the
  package version.
- A failed or timed-out `openapi` attach closes its HTTP client.
- `openapi` coerces numeric strings for `integer`/`number` parameters.
- `python-dir`: two directories shipping a module of the same name (say
  `helpers.py`) each get their own; sibling imports must be at module top level.
- A JSON Schema whose `type` is a list (a union) no longer raises `TypeError`
  in argument checking.
- `catalog-import` refuses a non-UTF-8 document instead of crashing.
- Calendar providers evicted from the per-identity cache are closed once no
  call is using them, instead of leaking their HTTP clients.

### Security

- `cryptography` 50.0.2 and `pyjwt` 2.15.1, clearing the 16 advisories
  `pip-audit` reported against the previous lock.

## [0.2.5] - 2026-10-06

### Changed

- **A role- or audience-gated surface hides its tools from a caller it
  refuses.** `tools/list` returns `{"tools": []}` to a caller who fails
  `[surface.authz]` `require_roles` or `audience`, a shared-token caller under
  `BEHEROUTER_AUTH_MODE=both` included, so an MCP host no longer offers its
  model tools that can only be refused. `initialize` and `tools/call` are
  unchanged: the call is still the gate and still names the missing role. The
  listing never errors and never materialises an identity. On by default;
  `[surface.authz] hide_tools = false` restores the old listing. Hosts cache
  the list, so a role granted mid-session shows after a reconnect.

- **One backend can no longer take the gateway down.** A surface whose attach
  fails, or exceeds `BEHEROUTER_ATTACH_TIMEOUT_S` (default 30 s), is served as
  an RFC 9457 `503` and retried in the background (5 s doubling to 300 s); on
  its first success it is swapped in and its tools array freezes as usual.
  `/healthz` stays HTTP 200 and reports `"status": "degraded"` with the
  surface under `failed`. Registry mistakes `registry-lint` can see still
  refuse boot.
- **FastMCP is capped below 4** (`fastmcp>=3.4,<4`); 4.0 moved modules
  plugins build on.
- ⚠️ **`sonarqube`'s credential is renamed `token` → `api_key`.** `token`
  derived `BEHEROUTER_<SURFACE>_TOKEN`, the name `client-config` gives the
  client's gateway bearer — two unrelated secrets under one name. A registry
  still setting `[<surface>.env] token = …` is refused at `registry-lint` and
  at boot, naming `api_key`; rename the key (the `${VAR}` it points at may
  stay, though `BEHEROUTER_<SURFACE>_API_KEY` is the name `plugin-config`
  emits).

### Added

- **`beherouter.plugin_api`**, the supported import surface for plugin
  authors, and `PluginSpec.api` (plugin API v1), which `register()` checks.
- **`openapi` plugin**: attach a REST API from its OpenAPI document — `include`
  lists the operations, schemas are closed, a per-user identity lands on the
  upstream request, and each call is isolated from the gateway caller's own
  request headers (FastMCP copies them by default). Lint checks the operation
  names offline.
- **`inproc` backing** and `plugin_api.load_inproc_backend` / `identity_client`
  / `mark_identity_aware`: a plugin can hand the gateway an in-process FastMCP
  server ("some decorated functions") in about 15 lines, and make it per-user
  by marking it with the `identity_client` its tools call out through. A call
  returns the tool's value exactly as an http/stdio backend would (a plain
  `str`/`list` return is not double-wrapped in FastMCP's `{"result": …}`).
- **`PluginSpec.requires_entry`**: a generic source can make `probe`/`pinned`
  mandatory, and `plugin-config` emits them. A plugin's `warn()` findings reach
  `registry-lint`'s `warnings`.
- **`python-dir` plugin**: a directory of `@tool`-decorated Python functions
  becomes a surface. It runs operator code in the gateway process. An import
  failure, a duplicate tool name or an empty directory is refused by file name,
  never skipped. `registry-lint` parses the files without importing them.
- **`register(..., published=)`**: a source reports the tool names its config
  publishes, and `validate_entry` refuses a `pinned` or `probe` outside them.
  `openapi` now also refuses a `probe` outside `include`.

### Fixed

- `beherouter attach`/`detach` no longer corrupt nested registry tables: a
  `[surface.identity.map]` was being rewritten as a string, silently
  breaking per-user identity on every other surface.

## [0.2.4] - 2026-09-25

### Changed

- **`search_tools` returns one-line briefs.** Each hit is `{name, brief,
  mutating?, pinned?}` — the first sentence of the description, capped at 160
  characters — instead of the full description plus annotations. On Plane a
  search fell from ≈4 300 tokens to ≈130. The full description and the
  argument schema are `describe_tool`'s job. Default `limit` is 5.
- **Precision.** Queries are normalised (stopwords, plurals, camelCase), tool
  names and first sentences weigh more than the rest, prefix and typo matches
  score instead of only back-filling, and weak hits are cut — by score, and by
  how much of the query they match. Scoring is BM25Plus rather than BM25Okapi,
  whose IDF went to zero for words most tools share. MCP tools are now indexed
  by their real argument names, enums and descriptions — previously their JSON
  Schema keywords were indexed instead.
- **`describe_tool`** no longer sends `callable` or empty `annotations`/`returns`.
- The `beherouter search` verb emits the same hits as `search_tools`, and takes
  `--limit`.

### Added

- **`search_aliases`**: per-plugin search vocabulary (Plane ships words such as
  `sprint` → `cycle`, `ticket` → `workitem`), extendable per surface under
  `[surface.search_aliases]`. `registry-lint` and `health --deep` warn on words
  for tools a surface does not serve.
- **Search quality gates** against Plane's recorded catalogue
  (`tests/test_search_eval.py`, `scripts/record_catalogue.py`).

### Fixed

- **`run_tool` argument handling matches pinned tools**: `None` values and
  schema-default echoes are dropped (the `archive=True`-on-`create` failure
  class), both argument spellings are accepted, and a missing required or
  undeclared argument is refused with an actionable `UsageError`.
- An unknown tool name in `describe_tool` / `run_tool` is a `NotFound` tool
  error with "did you mean" suggestions, instead of a successful
  `{"error": ...}` result.

## [0.2.3] - 2026-09-25

### Added

- **`/metrics`** (unauthenticated, like `/healthz`; counters only, labelled by
  reason and never by caller): `beherouter_auth_rejections_total{reason}`, with
  `reason` one of `expired`, `invalid`, `issuer`, `audience` or `scope`.
- **Auth rejections say why.** An expired token — the typical first incident of
  a per-user rollout — is logged at WARNING and answered with an RFC 6750
  `WWW-Authenticate: Bearer error="invalid_token", error_description="token
  expired"` instead of a generic 401 indistinguishable from a forged one.
- **Per-surface audience.** `[surface.authz] audience` narrows a surface to
  tokens whose `aud` names it, in addition to (never instead of) the
  gateway-wide verification, so one team's token is not accepted by another
  team's surface on the same gateway. The gateway-wide
  `BEHEROUTER_OIDC_AUDIENCE` may now be a comma-separated list (any of them).
  Like `require_roles`, the gate covers the meta-tools, refuses shared-token
  callers, and is refused at boot and at lint on an `auth.mode: shared`
  gateway; unlike it, it needs no roles claim.
- **Probe as a user.** `beherouter health --deep --bearer-file <file|->` runs
  each probe with a supplied user's token — same verifier, same gate, then the
  probe carrying the caller's identity — and reports the identity the backend
  returned plus `matches_caller`. `mismatch` is the silent
  everyone-acts-as-the-deployment-identity incident, made loud. The token is
  read from a file or stdin, never from argv.
- **Plane edition guard.** All three Plane plugins take `edition` (default
  `community`). On Community Edition, `workitem list` without `project_id`
  404s and any `pql` 400s — errors a model reads as transient and retries. The
  gateway now refuses those calls pre-transport with a non-transient message
  naming the fix, and appends the caveat to `workitem`'s description.
  Generic mechanism: `McpBacking.guard` / `McpBacking.notes`
  (`docs/PLUGINS.md`).
- **`contrib/plane-mcp-bearer`**: upstream `plane-mcp-server` 0.3.2 behind a
  verifier that forwards the caller's bearer to Plane as `Authorization:
  Bearer` (PAT-shaped tokens go as `X-Api-Key`), pinned to exactly that
  version because it rides upstream's private `auth_method` routing. This is
  the backend the `plane-http` plugin's identity mode `bearer` needs.
- **Chart: private CA and out-of-tree plugins.** `caBundle` appends a
  ConfigMap's PEMs to the system bundle for the gateway, its stdio children
  and the lint hook (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`). `plugins.install`
  installs `beherouter.plugins` distributions onto `PYTHONPATH` for the
  gateway **and** the lint hook, via `python -m beherouter.plugininstall`,
  which constrains every distribution shared with the gateway to its exact
  version and prunes the copies — a conflicting plugin fails its init
  container instead of shadowing the gateway's dependencies.
  `extraInitContainers` / `extraVolumes` / `extraVolumeMounts` reach both
  pods too. Chart packaging 0.1.4.
- **Image hardening.** The published image runs as UID 1000 (`USER` numeric,
  verifiable by `runAsNonRoot` without a passwd lookup) and ships
  `plane-mcp-server` 0.3.2 at `/opt/plane-mcp` — the `plane` stdio plugin's
  default `cmd` — so the plugin attaches on defaults. Opt out with
  `--build-arg PLANE_MCP_VERSION=`.
- **A missing stdio command is refused by name**, at attach with the override
  spelled out and as a `registry-lint` warning, instead of an OSError from
  inside the stdio client wrapped in "could not attach".
- stdio children inherit the gateway's trust-store variables, which matters
  behind a private CA: `requests` (the Plane SDK) reads `REQUESTS_CA_BUNDLE`
  and ignores `SSL_CERT_FILE`.
- `docs/AUDIT-2026-09-22.md`: the code audit behind several of the above.

### Changed

- **`plane-http` has no default `base_url` any more.** The old default pointed
  at upstream's OAuth proxy mount, which 401s a forwarded IdP token before
  Plane is consulted — a surface built from it attached green and failed every
  user call. `base_url` is required, and `validate()` refuses both upstream
  mounts by path.

### Verified

- 703 tests; the local end-to-end stack (`tests/e2e/`) grew to 22 checks and
  two new surfaces (`plane-bearer` through the contrib wrapper, `plane-stdio`
  on the image's default cmd): an IdP JWT reaching the Plane API as `Bearer`
  with the deployment PAT as `x-api-key`, the audience refusal, the CE guard
  message, the RFC 6750 expired 401 and its `/metrics` counter, and the
  bearer-file probe proving the caller's identity at the backend.

## [0.2.2] - 2026-09-22

Per-user Plane over HTTP (`plane-http` and `plane-http-apikey`, one shared pin
list), the role gate extended to the read-only meta-tools, `registry-lint`'s
warnings array naming the client-bearer/backend-credential collision,
out-of-tree plugins via the `beherouter.plugins` entry-point group, and a
local e2e stack proving two callers acting as themselves against real
`plane-mcp-server` 0.3.2. First release publishing the chart as an OCI
artifact (packaging 0.1.3), with the chart's lint hook rendering the auth
mode in every scenario.

## [0.2.1] - 2026-09-22

Per-user identity: OIDC accepted beside the shared token, four per-surface
identity modes, `[surface.authz]` role gating, per-identity providers for the
native calendar surfaces, and chart wiring for all of it (`identityMap`
mount, conditional gateway token, `ci/identity.yaml` scenario). Chart
packaging 0.1.2.

## [0.2.0] - 2026-09-17

First tagged release: tag-driven `release.yml` running the full suite on the
tagged revision, a tag/pyproject/appVersion consistency gate, and the
multi-arch image published to `ghcr.io/behemotion/beherouter`.

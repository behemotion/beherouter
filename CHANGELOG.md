# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/):
one section per release, newest first, and the section for the version being
released is the body of its GitHub Release (`release.yml` extracts it, and the
auto-generated contributor appendix is appended below it). Releases before
0.2.0 were not tagged; their history is the git log.

## [Unreleased]

### Changed

- **One backend can no longer take the gateway down.** A surface whose attach
  fails, or exceeds `BEHEROUTER_ATTACH_TIMEOUT_S` (default 30 s), is served as
  an RFC 9457 `503` and retried in the background (5 s doubling to 300 s); on
  its first success it is swapped in and its tools array freezes as usual.
  `/healthz` stays HTTP 200 and reports `"status": "degraded"` with the
  surface under `failed`. Registry mistakes `registry-lint` can see still
  refuse boot.
- **FastMCP is capped below 4** (`fastmcp>=3.4,<4`); 4.0 moved modules
  plugins build on.

### Added

- **`beherouter.plugin_api`**, the supported import surface for plugin
  authors, and `PluginSpec.api` (plugin API v1), which `register()` checks.

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

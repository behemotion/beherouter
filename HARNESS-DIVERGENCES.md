# HARNESS-DIVERGENCES — beherouter

> This repo vs the harness standard. Standard:
> `BEHEMOTION/docs/CONVENTIONS.md` · Roadmap:
> `BEHEMOTION/docs/HARNESS-PLAN.md` (Phase 2, items 7–8). The umbrella repo is
> not published; both are named rather than linked.
> Audited 2026-07-03; updated 2026-07-28 (build), 2026-07-29 (deployment),
> 2026-07-30 (post-deployment cleanup), 2026-09-09 (two surfaces attached) and
> 2026-09-10 (cache-hint decision recorded).
> Remove entries as they are fixed.

beherouter is the keystone: an authenticated gateway fronting many MCP backends. The
existential divergence (zero code) is **closed** — the gateway is built, green,
and **live** at `https://beherouter.example.com`. Both interface divergences that
mattered (no `/healthz`, no backend health contract) are closed too, and as of
2026-09-08 it fronts **two real backends** (`office`, `plane`) with a consumer on
each. **No interface divergence against CONVENTIONS remains open.** What is left
here is operational hardening, not contract work.

## Resolved by the 2026-07-28 build

- ~~**Design only, 0 implementation.**~~ The 14-task plan is executed through
  Task 13. 90 tests pass; `beheaxi conformance "beherouter"` → 6/6; both backend
  kinds attach against real backends.
- ~~**pyproject is a stub.**~~ Now carries the beheaxi git dep, `rank-bm25`,
  `jsonschema`, `[project.scripts] beherouter`, `[tool.uv.sources]`, and a ruff
  config matching beheaxi's.
- ~~**cli-kind deferral is invisible.**~~ Superseded 2026-08-04: the deferral
  itself is gone. `cli` backends execute (`CLIExecutor` runs the verb via
  `asyncio.create_subprocess_exec`, maps beheaxi exit codes 1–6 onto error
  classes and returns codes ≥10 as data), so `describe_tool` now reports
  `"callable": true` unconditionally and the note is deleted.

## Resolved by the 2026-07-29 deployment

- ~~**Not deployed.**~~ **Live since 2026-07-29** (as `behemcp.example.com` until the
  2026-08-02 rename), now at **`https://beherouter.example.com`** —
  on the **service** VM (198.51.100.114), not dev-4, and deployed from the
  **homelab** repo (`$HOMELAB_REPO/ur/service/beherouter/`) rather than by
  the retired `deploy.md`'s hand-run dev-4 procedure. `$DEV4_SSH_PASS` was never needed.
  DNS, Caddy vhost, systemd user unit + linger, node_exporter unit series and a
  Prometheus blackbox probe on `/healthz` are all in place and verified live.
- ~~**The container image needs beheaxi > v0.1.0.**~~ Closed by **vendoring**
  beheaxi (with the fix) into the homelab deployment instead of waiting on a tag.
  `beheaxi conformance "beherouter"` is **6/6 inside the deployed image**, including
  the `usage_exit_2` check that previously failed there. *(The fix itself was
  released as v0.1.1 on 2026-07-30 — see the next section.)*
- ~~**Two credential/health gaps found at deploy time.**~~ Both were real code
  defects and are fixed here, with tests: `build_transport` never passed `env` to
  `StdioTransport` while the MCP SDK scrubs a subprocess's environment down to six
  safe vars — so a stdio backend's credentials never arrived and every tool call
  failed while list/search looked healthy; and the gateway had **no `/healthz`**
  despite CONVENTIONS mandating one, which left no unauthenticated endpoint for
  monitoring to probe. 98 tests pass.

## Resolved by the 2026-07-30 cleanup

*(Design record: `docs/superpowers/specs/2026-07-30-beherouter-cleanup-design.md`.)*

- ~~**`gitea-home`'s Gitea PAT is dead.**~~ Closed by **removing the surface**
  rather than rotating the token — nothing was consuming it yet, and it comes
  back deliberately, with a `probe`. The ledger entries for the revoked PAT and
  for that surface's per-client Caddy token are annotated as retired, and the
  surface's now-dead vault key is removed by the 2026-08-02 rename.
- ~~**No backend health contract.**~~ `beherouter health --deep` attaches each
  backend fresh and calls the `probe` tool named by its registry entry, reporting
  `{attach, probe, error}` per backend and exiting `Unavailable` (6) if any
  failed. This is the check whose absence let a revoked PAT read as green for a
  day: `tools/list`, `search_tools` and `describe_tool` are all answered from the
  attach-time catalogue, so only a real `tools/call` touches the credential.
  `probe: none`/`unsupported` report as **unknown, never green**. `/healthz`
  stays shallow on purpose — a fan-out would be as slow and as flaky as the
  slowest backend and would turn one backend's outage into a gateway alarm.
  *(Plan Phase 2, item 8.)*
- ~~**The container image needs beheaxi > v0.1.0** (second half).~~ The fix is
  released: **beheaxi v0.1.1**, tagged and pushed, pinned in `pyproject.toml`.
  A plain build from this repo no longer reproduces the `usage_exit_2` failure.

## Resolved by the 2026-08-04 and 2026-09-08 attachments

- ~~**No surfaces are attached at all.**~~ Closed twice over: **`office`**
  (2026-08-04, office-mcp over the shared `behe-gateway` network) and **`plane`**
  (2026-09-08, plane-mcp-server as a stdio subprocess). Both report `attach: ok` +
  `probe: ok` under `health --deep`, `/healthz` →
  `{"status":"ok","surfaces":["office","plane"]}`, and `office` carries real
  production traffic — so the mcp path **is** exercised outside the test suite now.
  Each surface has a named consumer (Mac clients + hermes for `office`, LibreChat for
  both), which was the gate the previous Plane wrapper failed: it recorded zero calls
  ever before being removed. `plane` is also the **first surface with a backend
  credential**, proving the `${VAR}`-in-`registry.toml` indirection end to end.
  Details and traps: `AGENTS.md` § Attached surfaces.

## Caching hints (`ttlMs` / `cacheScope`) — deliberate, 2026-09-10; harness surveyed 2026-09-11

`BEHEMOTION/docs/CONVENTIONS.md` does not yet carry the 2026-07-28 MCP caching utility,
and `mcp` 1.29.0 does not type it: `ListToolsResult` carries only `meta`,
`nextCursor` and `tools` (`ListToolsResult.model_fields` checked directly
against the installed package). beherouter uses a configured TTL
(`catalogue_ttl_ms`, default 300 000 ms) instead of backend-supplied hints.

Honoring hints would require `list_tools_mcp()` in place of `Client.list_tools()`
— i.e. hand-rolling the cursor-pagination loop FastMCP currently gives us for
free, including its `max_pages=250` guard and duplicate-cursor detection. That
is a real, permanent cost for a signal nothing here is known to send.

**Evidence base — all three MCP backends, surveyed 2026-09-11.** The original
2026-09-10 pass reached only `gitea-mcp` (stdio, installed locally, already
exercised by this repo's gitea-gated integration tests): 50 tools, `meta: None`
at the top level and on every tool. The two live surfaces were then queried
from **inside the `beherouter` container on the service VM**, because that is
where `registry.toml` and `BEHEROUTER_PLANE_TOKEN` live — both are gitignored
deploy state, absent from a clone. (The *other* reason given on 2026-09-10, the
workstation egress gate, no longer applies: see the note at the end of this
section.) `list_tools_mcp()` returned:

| Backend | Transport | Tools | Top-level `meta` | Per-tool `meta` |
|---|---|---|---|---|
| `gitea-mcp` | stdio | 50 | `None` | `None` on all 50 |
| `office` | http | 4 | `None` | `None` on all 4 |
| `plane` | stdio | 30 | `None` | **non-null on all 30** — `{"fastmcp": {"tags": []}}` |

⚠️ **`plane`'s non-null `_meta` is NOT a caching hint.** It is FastMCP's own
tag metadata, emitted by the server framework `plane-mcp-server` is built on.
There is no `ttlMs` and no `cacheScope` anywhere in it. The trap this leaves
behind is a detector: "is `meta` None?" was a sufficient check when only
`gitea-mcp` had been seen, and is now wrong — a hint check has to look for the
specific keys, because a FastMCP-based backend always carries *something*
there.

**What ignoring the hint actually costs.** Our TTL is fixed and ours, not
backend-supplied, and no backend has been observed asking for anything
different. The case we expect is a hint asking us to refresh *sooner* than
`catalogue_ttl_ms` already does — a backend whose tools change faster than our
300 000 ms default would want a shorter bound than we give it — but a backend
could equally send a `ttlMs` *larger* than 300 000, which we would simply be
refreshing past. We have not verified our default beats every possible hint
value; what we are giving up by not reading one is a chance to shrink that
window, not a safety margin we can claim in advance.

**Revisit when:** the `mcp` SDK types the caching utility, or a backend is
observed emitting `ttlMs`/`cacheScope` — re-survey on a backend upgrade, since
this is a property of the server, not of its catalogue.
`catalogue.Catalogue.ensure_fresh` is the single place to change.

> ⚠️ **The workstation egress gate is gone, 2026-09-11.** `AGENTS.md` § Next
> steps 2 states that this Mac cannot reach `198.51.100.x` from Python
> (`No route to host`) and that only system `curl` works. That is no longer
> true: a full `fastmcp` Client session ran from the Mac to
> `https://beherouter.example.com/office/mcp` (which resolves to 198.51.100.114),
> completing `initialize`, `tools/list` and real `tools/call`s. Raw sockets
> reach 198.51.100.114 on both 443 and 80, and 198.51.100.1:443 answers
> `ConnectionRefused` — packets arrive and are refused, which is not the
> `EHOSTUNREACH`/`No route to host` the gate produced. Both entries above cite
> that gate as a reason for measuring by proxy or on the VM; the durable reason
> is the one restated inline — `registry.toml` and the credentials are
> VM-only — not reachability.

## Catalogue refresh TTL — measured against both live surfaces, resolved 2026-09-11

The design spec's Open Question 2 asked that the `catalogue_ttl_ms` default be
validated against a real re-list before shipping, because a refresh slow
enough to be felt inside a `search_tools` call would have to move off the
request path — a design change, not a tuning knob. Both live surfaces
(`office`, `plane`) inherit `PluginSpec.catalogue_ttl_ms = 300_000` with no
override.

**Measured 2026-09-11, from inside the `beherouter` container on the service
VM** — where `registry.toml` and the Plane PAT actually live. A re-list is
`async with Client(transport): await client.list_tools()` (`load_mcp_backend._relist`),
timed five times per surface against the same transport, i.e. warm:

| Surface | Backing | Tools | Attach | Warm re-list |
|---|---|---|---|---|
| `office` | http | 4 | 0.076 s | **0.016–0.023 s** |
| `plane` | stdio | 30 | 1.78 s | **0.005–0.007 s** |

Both are roughly two orders of magnitude under the ~1 s threshold that would
force the refresh off the request path, so the 300 000 ms default stands on
real evidence and the refresh stays inline. This supersedes the 2026-09-10
`gitea-mcp` proxy measurement (~0.005 s), which the real `plane` figure
confirms rather than corrects.

⚠️ **Attach is not re-list.** `plane`'s 1.78 s attach is a cold stdio
subprocess start and is paid once, at gateway startup; the 0.005 s figure is
what a `search_tools` call past the TTL actually pays, because `keep_alive=True`
holds that subprocess warm.

**Why this was safe to ship before the measurement.** The degrade path is the
same one `catalogue.Catalogue.ensure_fresh` always takes: a failed or slow
re-list serves the last-good catalogue and reports `stale` rather than blocking
or erroring, so an unmeasured re-list cost was a staleness risk, not a
correctness or availability one.

**Re-measure when:** either backend is upgraded, `plane`'s catalogue grows
substantially, or a third `mcp` surface is attached.

## Per-user identity — the gateway can carry one, no live surface uses one yet

Resolved 2026-09-22 by `docs/superpowers/specs/2026-09-21-per-user-identity-design.md`,
prompted by an external feature request from a team running the published chart
in front of Plane CE with ~2100 directory accounts behind LibreChat.

**What was divergent.** The gateway had exactly one identity. `SharedTokenVerifier`
was hardcoded at boot, so every caller was the same caller; every backend call used
the deployment credential; and `build_transport` accepted a `headers` argument that
**nothing populated** and that its `stdio` branch **never read** — so a per-user
configuration, had one been expressible, would have attached green and forwarded
nothing.

**What now exists.** `BEHEROUTER_AUTH_MODE=oidc|both` accepts a JWKS-verified JWT
beside the shared token; a surface opts into forwarding with `[surface.identity]` in
one of four modes (`bearer`, `claims`, `client`, `lookup`); `http`, `cli` and
`native` apply it and `stdio` refuses it in three places. `[surface.authz]`
`require_roles` gates a surface on the caller's roles, separately, so a surface may
gate while forwarding nothing. Full mechanism: [`docs/IDENTITY.md`](docs/IDENTITY.md).

**What is still open, and is the honest residual:**

1. **No live surface uses any of it.** `office` and `plane` are attached with no
   `[identity]` table, so every write through them is still attributed to one
   identity. The mechanism is tested, not exercised in production.
2. ~~**`plane` cannot use it as it stands.**~~ Resolved 2026-09-22: `plane-http`
   and `plane-http-apikey` attach the same pin list over HTTP, and per-user Plane
   is proven end to end in `tests/e2e/` — two callers, two Plane identities,
   through real plane-mcp-server 0.3.2. The live surface is still the stdio
   `plane`, so nothing in production is per-user yet.

   ⚠️ **The residual is which mount.** `/http` is an OAuth proxy that accepts
   only tokens it minted itself and 401s a forwarded one before Plane is
   consulted; `/http/api-key` takes a per-request PAT and works today. So
   per-user Plane against the published server means **a PAT per caller**
   (mode `client`, nothing stored by the gateway), not an IdP token. Mode
   `bearer` against Plane needs a backend that accepts a forwarded token:
   since 2026-09-24 that is `contrib/plane-mcp-bearer` (proven in e2e), which
   is pinned to exactly plane-mcp-server 0.3.2 because it relies on upstream's
   private `auth_method` routing. An upstream bearer-forwarding mount would
   retire it; none has been requested yet.
3. **No end-to-end verification against a real IdP has been performed here.**
   Every rule is held by a test with a locally-minted key pair. A production
   deployment outside this harness reports Keycloak JWTs verified by JWKS in
   `auth.mode: both` (2026-09-24); no such JWT has traversed THIS deployed
   gateway.
4. **The catalogue and the probe stay deployment-scoped by design.** A green
   `health --deep` proves the deployment credential and says nothing about any
   user's. The record now says so in its own output (`probe_scope`), which makes
   the limitation legible rather than removing it. Since 2026-09-24,
   `health --deep --bearer-file` runs the probe as a supplied user and reports
   the identity the backend returned. That proves the per-user path on demand;
   it does not make the scheduled probe per-user.
5. **Modes `client` and `lookup` have no external consumer asking for them.** The
   requesting team wants `bearer` only. They were chosen deliberately and are
   tested; they are nonetheless unexercised by any stated need.
6. **A gated surface is still *listable*.** `search_tools`, `describe_tool`,
   `run_tool` and `context_cost` now refuse a caller the surface would not serve
   (and refuse before re-listing the backend), but the FROZEN published `tools`
   array is captured at attach and served to every authenticated caller. Closing
   that means a FastMCP `on_list_tools` middleware and a decision about what a
   host should be shown when it may call nothing — unbuilt, and deliberately so:
   the gate carries no security weight, the backend's own verification does.

## Open

1. ~~**No surfaces are attached at all.**~~ Resolved — see the section directly above.
   *(Slot kept so items 2–5 keep their numbers; `AGENTS.md` cites §5.)*

   ~~The residual worth naming: **two surfaces on one gateway is also two ways to take
   the gateway down.**~~ **Closed in code 2026-09-25 (Plugin sources Phase 0),
   unreleased** — the live 0.2.4 gateway still crash-loops until the next `/deploy`.
   A surface whose attach fails or exceeds `BEHEROUTER_ATTACH_TIMEOUT_S` is served as
   `503` and retried with backoff; `/healthz` stays up and names it under `failed`, so
   it now says *which* backend broke. What `registry-lint` can see (unknown plugin,
   bad config, unset `${VAR}`) still refuses boot, deliberately. Held by
   `tests/test_gateway_isolation.py`.
2. **Pins are now tested knowledge; nothing still validates them against the LIVE
   catalogue.** Partly closed 2026-09-09 by Plugins Phase 1.

   *Closed:* pins no longer live as prose in another repo's `registry.toml` comments.
   They are `PluginSpec.pinned` in `src/beherouter/plugins/`, and the two dead ends
   the 2026-09-08 `plane` attach found are enforced by tests —
   `test_plane_pins_exclude_community_edition_gaps` (the five Community Edition
   404s) and `test_plane_does_not_pin_get_pql_reference` (uncallable upstream in
   0.3.2). A registry entry that omits `pinned` gets the plugin's *verified* list
   rather than nothing, so the pin list is no longer knowledge an operator has to
   carry. `registry-lint` checks an entry offline before it can crash-loop the
   gateway.

   *Still open, unchanged:* **nothing validates a pin against the live catalogue at
   attach time.** gitea-mcp v1.4.0 consolidated a 106-tool catalogue into 53
   action-parameterized tools; a pinned name that no longer exists is **silently not
   pinned** — the surface still builds and still serves search/describe/run, so the
   only symptom is a short `tools/list`. Hit for real during the 2026-07-29
   deployment.

   **And validating names would not be enough.** A pinned name can exist, list
   cleanly, and still be a dead end: `page` because this Plane is the Community
   Edition and its REST endpoint 404s (an edition boundary, not a permission), and
   `get_pql_reference` because its schema declares `detail` while its dispatcher
   demands an `action` the schema forbids. Neither is visible to `tools/list`.
   Validating names is the floor; the real contract is *call each pin once*. Until
   something automates that, the rule is manual and stated in `AGENTS.md` § Plugins:
   **probe before pinning, re-probe after any backend upgrade or edition change.**
3. **The deployment source is a vendored copy** — now a *pinned mirror*, which is
   the deliberate choice rather than an accident. `$HOMELAB_REPO/ur/service/beherouter/`
   holds a copy of this repo's `src/` and of beheaxi at v0.1.1; un-vendoring
   would make the host build fetch a **private** GitHub repo and therefore need a
   token build-secret, which is worse than keeping a verifiable copy. Guardrails:
   the vendored `pyproject.toml` floors `beheaxi>=0.1.1` so a stale re-vendor
   fails the build, and drift is checkable with
   `diff -rq src $HOMELAB_REPO/ur/service/beherouter/src --exclude=__pycache__`.
   Changes must still be made **here** and re-vendored, or they are overwritten.
4. **The manifest contract is closed** (`additionalProperties: false` at every
   level of beheaxi's schema): any sibling emitting an extra key fails attach
   with exit 6. Intentional; now stated in `src/beherouter/manifest.py` and pinned
   by `test_extra_key_is_rejected`. Keep saying it loudly in registration docs so
   layer authors extend the schema *in beheaxi*.
5. **Attachability reality check** (2026-07-03 audit, still current *for siblings*):
   the gateway can front `gitea` (external, proven live — 53 tools on v1.4.0, though
   not currently attached) + `behecheck` (`mcp/stdio`, untested here).
   behemem/behetask still have nothing to point `registry.toml` at; behelib's
   hand-rolled JSON-RPC (protocol `2024-11-05`) is a risky `mcp/http` mount — prefer
   waiting for its beheaxi migration (plan Phase 3). Don't let backend scarcity look
   like a gateway bug.

   ⚠️ **Read this as a statement about *siblings*, not about the gateway.** Both live
   surfaces (`office`, `plane`) are **third-party backends, not BEHEMOTION layers** —
   which is why the gateway got real traffic long before any sibling became
   attachable. The scarcity this item describes is a harness-adoption fact; it never
   blocked the gateway, and it should not be quoted as if it did.

## Notes

6. `HANDOFF.md` map lists only beherouter — populate the full sibling map (plan
   Phase 1, item 4). A local `docs/handoffs/` tree now exists and is
   **deliberately untracked** (chosen 2026-09-09); the tracked home for
   self-handoffs remains the umbrella's `docs/handoffs/self/`.
7. ~~Live `cli` execution~~ landed 2026-08-04. ~~LibreChat/Hermes wiring is deferred
   by the spec (§10).~~ Closed: `beherouter client-config <agent>` emits a real
   dialect for **all five** consumers — librechat, claude-code, pi, opencode and
   **hermes** (added 2026-08-08 and verified by a live agent turn calling
   `search_tools("discovr")` through the gateway, not merely by config inspection).
   Four are verified by an agent tool call; LibreChat's `office` paste is the one
   step still awaiting a human. The generator never emits a credential, only a
   placeholder.

   ⚠️ **`codex` is NOT a consumer and must not be given a dialect.** It is not used in
   this homelab and is not installed on the Mac. An earlier version of this note named
   it as one, and that error alone regenerated a phantom "Codex dialect" work item
   across three handoffs. The consumer set is exactly the five above.

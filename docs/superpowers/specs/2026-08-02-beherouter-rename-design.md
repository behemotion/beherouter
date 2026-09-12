# behemcp → beherouter: identity rename (2026-08-02)

> **Status: design, approved for planning.** Scope is the rename only. The
> CLI-routing capability that motivates the new name is a *separate* project
> with its own brainstorm → spec → plan cycle, executed on the renamed base.

## Why

The layer is outgrowing its name. `behemcp` names a mechanism (MCP); the layer
is becoming a **router** — an MCP gateway *and* a CLI router. Renaming now,
while the surface set is empty and no consumer is wired, is the cheapest this
will ever be. Every later attached backend, LibreChat entry and client token
raises the cost.

## Non-goals

- **No CLI-router design.** What CLI routing means, how it composes with the
  MCP surfaces, and whether `gateway.py` should be renamed to match are
  deliberately out of scope. This spec changes the *name*, not the shape.
- **No git history rewrite.** No rebase, no force-push. "Rewrite everything"
  (below) means the archived *documents*, not the commit graph.
- **No compatibility shim.** No `behemcp` alias binary, no `BEHEMCP_*` env
  fallback. There are no consumers to protect — zero surfaces attached, no
  LibreChat/Hermes wiring — so a shim would be pure carrying cost and would
  violate `docs/CONVENTIONS.md` §1 (one layer = one name, everywhere) the
  moment it shipped.

## Decisions taken

| Question | Decision |
|---|---|
| Scope | Rename only; CLI router deferred to its own cycle |
| Live endpoint | **Hard cut** to `beherouter.example.com`; old vhost + DNS record deleted, no redirect |
| Archive documents | **Rewrite everything** — dated specs, plans, audits and handoffs get both filenames and bodies updated |
| Execution | **Staged**, three independently verifiable phases |

## Name mapping

`docs/CONVENTIONS.md` §1 makes the mapping mechanical once the slug is chosen:

| Facet | Old | New |
|---|---|---|
| Repo slug / directory | `behemcp` | `beherouter` |
| Python package | `src/behemcp/` | `src/beherouter/` |
| `[project] name` | `behemcp` | `beherouter` |
| CLI binary (`[project.scripts]`) | `behemcp` | `beherouter` |
| beheaxi app `name=` | `behemcp` | `beherouter` |
| Env-var prefix | `BEHEMCP_` | `BEHEROUTER_` |
| Gateway token env var | `BEHEMCP_GATEWAY_TOKEN` | `BEHEROUTER_GATEWAY_TOKEN` |
| Registry path env var | `BEHEMCP_REGISTRY` | `BEHEROUTER_REGISTRY` |
| Default registry path | `~/.config/behemcp/registry.toml` | `~/.config/beherouter/registry.toml` |
| In-container registry mount | `/etc/behemcp/registry.toml` | `/etc/beherouter/registry.toml` |
| Auth `client_id` | `behemcp-shared` | `beherouter-shared` |
| Caddy vhost | `behemcp.example.com` | `beherouter.example.com` |
| GitHub repo | `behemotion/behemcp` | `behemotion/beherouter` |
| Container image / compose / unit | `behemcp`, `behemcp-compose.service` | `beherouter`, `beherouter-compose.service` |
| Host dir on service VM | `~/behemcp/` | `~/beherouter/` |
| Ansible tag | `--tags behemcp` | `--tags beherouter` |
| Vault key | `<VAULT_KEY-old>` | `<VAULT_KEY>` |
| Homelab vendored dir | `ur/service/behemcp/` | `ur/service/beherouter/` |
| Env template | `behemcp-env.j2` | `beherouter-env.j2` |

**Unchanged:** app port **47100**, the surface-name namespace (`/<surface>/mcp`),
`registry.toml` as the filename, and the beheaxi pin
(`beheaxi @ git+https://github.com/behemotion/beheaxi@v0.1.1`).

## Blast radius

Roughly 330 textual occurrences across three repositories.

**This repo (37 files).** Only 11 lines are in `src/`, and only four are
behavioural: `auth.py:18` (`ENV_VAR`), `auth.py:59` (`client_id`),
`cli/app.py:20` (the beheaxi app name — the string `beheaxi conformance`
asserts), `cli/app.py:28` (`BEHEMCP_REGISTRY` + config-path default). The
remainder is docstrings, tests, container files, and ~200 prose hits in
`AGENTS.md`, `README.md`, `deploy.md`, `HANDOFF.md`, `HARNESS-DIVERGENCES.md`,
`docs/DESIGN.md`, `docs/FASTMCP-NOTES.md` and the `docs/superpowers/` archive.

**Umbrella + siblings (50 files).** `README.md` catalogue and adoption matrix,
`HANDOFF.md` registry, `docs/CONVENTIONS.md` (four hits including the port
registry line), `docs/HARNESS-PLAN.md` (Phase 2 items 7–8),
`docs/harness-architecture.drawio` **and** the regenerated
`docs/harness-architecture.svg`, `AUDIT-2026-06-21.md`, the `docs/handoffs/`
tree, and single-line mentions in all eight sibling repos. `beheaxi`'s two
source hits (`conformance/checks.py:5`, `tests/test_app_errors.py:45`) are
comments only — no code dependency exists in either direction.

**Homelab (live).** `ur/service/behemcp/` (vendored `src/` + vendored beheaxi +
`registry.toml` + compose + Containerfile), `ur/service/Caddyfile` (the vhost
block at line 138 and a cross-reference at 241), `ur/service/AGENTS.md`,
`ansible/playbooks/service.yml` (tag, role invocation, `compose_name`, sync
tasks), `ansible/playbooks/templates/behemcp-env.j2`, the vault key,
`monitor/prometheus/prometheus.yml:223`, and the MikroTik DNS record.

## Architecture of the change

Three phases, each ending in a state provable by a command. Phase 1 is fully
reversible and touches nothing deployed; the live service keeps serving the old
image throughout. Phase 2 is the only phase with an outage window, and it is
structured new-before-old so the window is zero.

### Phase 1 — upstream repo

Rename the package, the entry point, the env vars, the container files and all
prose; rewrite the `docs/superpowers/` archive (file names and bodies);
reinstall; verify; rename the GitHub repo and the local working directory.

The ordering constraint that matters: `tests/test_integration.py` and
`tests/test_cli_app.py` shell out to the **installed console script**, so the
`[project.scripts]` rename does not take effect until `uv sync` reinstalls the
package. Edit and reinstall belong to the same step, or the test run reports a
failure that has nothing to do with the edit.

Directory and GitHub renames come **last**, after tests are green, so that a
failure mid-phase never leaves the remote and the working copy disagreeing.
`[tool.uv.sources] beheaxi = { path = "../beheaxi" }` is a sibling-relative
path and survives the directory rename untouched.

**Verification:**
- `PATH="$PWD/.venv/bin:$PATH" pytest -q` → **115 passed** (matches today's
  baseline; the four "failures" seen without that PATH prefix are an artifact
  of `.venv/bin` not being on PATH, not real).
- `beheaxi conformance "beherouter"` → **6/6**.
- `grep -ril behemcp .` → no hits outside `.git/`.

### Phase 2 — homelab cutover

New-before-old, because the 2026-07-30 incident taught that the gateway and the
edge disagreeing is worse than either being down:

1. Re-vendor: `ur/service/beherouter/` from the renamed upstream `src/` (a fresh
   copy, not a `git mv` of the stale one — the existing `ur/service/behemcp/`
   carries a stray `.venv/` that should not be recreated).
2. Add the renamed vault key alongside the existing one; render
   `beherouter-env.j2`.
3. Add the `beherouter.example.com` **MikroTik static DNS record**. This is the one
   step no playbook performs — `*.example.com` resolves through per-hostname static
   records on 203.0.113.1, not a wildcard. TLS needs nothing: the Let's Encrypt
   `*.example.com` wildcard already covers the new name.
4. Add the new Caddy vhost, leaving the old one in place for now.
5. Run **both** playbooks — `service.yml --tags beherouter` *and*
   `caddy.yml -l service`. `--tags` does not ship the Caddyfile; skipping the
   second is exactly how the edge and the gateway drifted apart on 2026-07-30.
   Cold builds need `-e compose_state=stopped` or the unit's
   `TimeoutStartSec=300` SIGKILLs the build.
6. **Verify the new endpoint before removing anything:**
   `curl -fsS https://beherouter.example.com/healthz` → `{"status":"ok","surfaces":[]}`
   and `podman exec beherouter beherouter health --deep --json` → exit 0.
7. Only then remove the old: Caddy vhost block, MikroTik record, `~/behemcp/`,
   `behemcp-compose.service` (stop + disable first), the old image, the old
   vault key, and `ur/service/behemcp/`.
8. Retarget the Prometheus probe to `https://beherouter.example.com/healthz`,
   preserving the comment explaining why it must point at `/healthz` and not a
   surface path.

The empty surface set makes this unusually cheap: with no surfaces attached the
Caddyfile reduces to "deny everything except `/healthz`", so there are **no
per-surface token clauses to migrate** and the three-edits-per-surface rule
(registry block + Caddy `not` clause + env token line) has nothing to
coordinate. The rule itself, and its warning, carry over verbatim into the new
vhost's comments.

**Verification:** new endpoint answers as above; `podman ps` shows only
`beherouter`; `systemctl --user list-units 'behe*'` shows no `behemcp-compose`;
the Prometheus target is green under its new name.

### Phase 3 — umbrella and siblings

Documentation only, no runtime effect, so it runs last and can be split across
commits per repo (each sibling is an independent git repo; per the umbrella
AGENTS.md, write in-tree and let each layer's owner commit).

`docs/harness-architecture.drawio` must be edited *and* its `.svg` regenerated —
the SVG is a build artifact with 14 baked-in hits, and editing only the source
leaves the rendered diagram lying. `docs/CONVENTIONS.md`'s port-registry line
(`behemcp 47100`) moves with the rest; the port itself does not change.

Per the "rewrite everything" decision, dated archives — `AUDIT-2026-06-21.md`,
`docs/handoffs/self/BEHEMCP-*.md`, the sibling repos' dated specs and plans —
are rewritten in place, filenames included. This is a deliberate choice to
favour a clean `grep` over archival fidelity; the trade-off is that documents
dated before 2026-08-02 will refer to a name that did not exist on their stated
date.

**Verification:** `grep -ril behemcp ~/Repo/BEHEMOTION $HOMELAB_REPO` returns
nothing outside `.git/` and `node_modules/`.

## Error handling and rollback

| Phase | Failure mode | Recovery |
|---|---|---|
| 1 | Tests fail after rename | `git checkout .` — nothing deployed changed |
| 1 | GitHub rename done, local push fails | GitHub serves redirects for the old URL; `git remote set-url` and retry |
| 2 | New endpoint doesn't answer | Old vhost, DNS record, unit and image are all still live and serving — abort before step 7, delete nothing |
| 2 | Failure *after* step 7 | Re-run both playbooks from the renamed sources; the service is stateless (config-file only, no DB), so there is nothing to restore |
| 3 | Anything | Documentation only; revert the commit |

The one genuinely irreversible action is the GitHub repo rename, and it is
cheap to reverse (rename back; GitHub redirects either direction).

## Testing

No new tests. The rename is verified by the existing suite continuing to pass
(115) plus the conformance suite continuing to report 6/6 under the new name —
which together prove the four behavioural lines were changed consistently
rather than partially. A partial rename is the realistic failure here: an
`ENV_VAR` renamed while `podman-compose.yml` still sets the old name would
start a gateway with no token, which `auth.py` already treats as a hard startup
error rather than an open gateway. That existing safeguard is what makes this
rename safe to do in one pass.

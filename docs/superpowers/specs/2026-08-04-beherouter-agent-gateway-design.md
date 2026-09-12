# beherouter as the agent gateway — connecting every backend, MCP and CLI

**Date:** 2026-08-04 · **Status:** design, approved in conversation · **Supersedes:** nothing

## The goal (marked)

> **Configure and adjust the whole infrastructure — every service we run — into
> any agent client (LibreChat, pi, Claude Code, OpenCode, Hermes) with the
> simplest setup possible.**

Two things follow from that sentence and bind every decision below:

1. **The consumers are agents, not humans.** Every surface is judged by whether
   an agent can discover and call it without a human reading docs first. A
   design that is elegant but needs per-client hand-tuning has failed the goal.
2. **Setup cost is a first-class metric.** "Add a service to all my agents"
   should be one registry block plus one generated config block — not N
   hand-edits across N client config files.

**Clean slate.** There are no existing users and no usage history to preserve
(confirmed 2026-08-04). Nothing below is constrained by backwards
compatibility, and beherouter **stays stateless** — no database, no volume.
`deploy.md` calls that absence the reason rollback is cheap; it survives.

## Non-goals

- **No usage/audit recording in the gateway.** It would require both a
  datastore and a per-user identity the gateway deliberately cannot see —
  Caddy rewrites every client's token to one shared gateway credential, and
  that rewrite *is* the isolation boundary.
- **No aggregate `/all` endpoint in this design.** Per-surface endpoints stay.
  An aggregate surface remains purely additive if it is ever wanted.
- **No CLI consumer mode.** beherouter does not grow a `beherouter call …`
  router for humans. CLI *backends* are exposed as MCP tools; MCP stays the one
  consumer protocol. (The one CLI addition is `client-config`, a setup helper,
  not a routing path.)
- **No embeddings, ever.** Unchanged hard constraint.

## Verified starting state (2026-08-04, measured not assumed)

- Live at `https://beherouter.example.com`, `{"status":"ok","surfaces":[]}`, zero
  surfaces attached. Conformance 6/6 in-image. Resource limits now
  `1g / 1.0 cpu / 256 pids`.
- **`cli` backends attach but cannot execute.** `backends/cli.py:47`
  `CLIExecutor.run()` unconditionally raises `Unavailable`;
  `surface.py:167` hardcodes `callable_backend = backend.kind != "cli"`.
- **Search is per-surface**, one `ToolIndex` per backend, built in
  `build_surface`.
- **The index holds `name + summary + verb` only** (`surface.py:162`).
- **`search_tools` returns bare `list[str]`**, forcing a `describe_tool`
  round-trip per hit.
- **Membership is exact token intersection** (`search.py:56`, `q & toks`). A
  typo or a morphological variant returns nothing.
- **The gateway container cannot reach its own host.** Measured from inside:

  | From `podman exec beherouter` | Result |
  |---|---|
  | `127.0.0.1:8100`, `host.containers.internal:8100`, `198.51.100.114:8100` | refused |
  | `https://office-mcp.example.com` (own host :443) | refused |
  | `https://gitea.example.com`, `https://memory-mcp.example.com`, `https://github.com` | **200** |

  DNS resolves correctly; it is the rootless-podman hairpin. Co-location is a
  liability, not a shortcut.

- **beheaxi manifest contract** (`describe.py:29-37`): required args are
  **positional (bare name)**, optional render as **`--flag-name`**. Args carry
  `name/type/required/enum` — **no per-arg description field**.

## Architecture

Everything is a backend of one of two kinds, and `surface.py` stops caring
which:

| Kind | Transport | After this work |
|---|---|---|
| `mcp` | stdio · streamable http | unchanged, already works |
| `cli` | beheaxi manifest | **callable** |

Non-MCP HTTP services (drawio-export, rag_api) become `cli` by acquiring a thin
beheaxi CLI. There is therefore exactly **one** new execution mechanism to
build, not three — and the manifest gives each one descriptions, arg types,
`mutating` flags and `pinned` defaults for free.

### Workstream A — make `cli` callable

Replace the stub; drop the hardcode. Four correctness requirements:

1. **argv from the manifest.** Positionals in declared order, then
   `--flag value` pairs; booleans as bare presence when true, omitted when
   false. `_normalize_args` already preserves the wire name, so the mapping
   exists.
2. **`asyncio.create_subprocess_exec`, never `subprocess.run`.** `run()` is
   `async` and on the request path — a blocking call stalls the event loop for
   *every* surface, not just this one. (`_describe` may stay blocking: it runs
   once, at attach.)
3. **Never `shell=True`, no string interpolation into a command.** Args are
   agent-supplied. Argument vector only.
4. **Exit-code mapping.** beheaxi reserves 0–9, domain codes are ≥10 (umbrella
   `CONVENTIONS.md`). Reserved codes map to gateway errors; domain codes are
   returned as tool results, because a domain failure is information for the
   agent, not a transport fault.

Also: a per-call **timeout**, and stdout parsed as JSON with a clear error when
a CLI emits non-JSON (the same failure `_describe` already handles at attach).

### Workstream B — search: descriptions and fuzzy

Three changes, all inside `search.py` / `surface.py`:

1. **Descriptions inline.** `search_tools` returns
   `[{name, summary, pinned, mutating}]` instead of `[str]`. This changes the
   published tool schema — **free now, breaking later**, and there are zero
   consumers today. Same reasoning that justified renaming while nothing was
   attached.
2. **Enrich the index** with arg names and enum values.
   ⚠️ **Ceiling:** beheaxi manifests carry no per-arg descriptions, so for
   `cli` backends that is as rich as it gets. Going further means changing the
   manifest schema, which is an **umbrella-level decision** (`docs/CONVENTIONS.md`
   owns it — "changes to a rule happen here first, never as local forks").
   MCP backends have per-property descriptions in JSON Schema and are indexed
   fully.
3. **Tiered fuzzy, embeddings-free.** Membership is decided in tiers — exact
   token match, then prefix/stem, then `rapidfuzz` edit distance — with each
   tier firing only when the previous under-fills `limit`. **BM25 keeps doing
   the ranking.** This preserves the existing (correct) decision that BM25
   scores rank but do not decide membership, documented at `search.py:40-49`.

### Workstream C — the hairpin

Put beherouter and the same-host backends on a **shared podman network** so it
can dial `office-mcp:8100` by service name. Homelab change, no gateway code.
Gates office-mcp *and* any beheaxi CLI that calls a same-host HTTP service.

### Workstream D — attachment and the setup story

Per-surface plumbing stays three edits (registry block, Caddy `not` clause, env
token line) with `probe` set from the start.

**New: `beherouter client-config <agent>`.** Emits a paste-ready config block
for a named agent client, covering every attached surface at once, generated
from `registry.toml`. This is what makes the marked goal real: adding a service
to every agent becomes *one registry block, then re-run the generator*, instead
of N hand-edits across N client files. It reads config and writes nothing, so
the service stays stateless.

**Targets in scope for Phase 4:** `librechat`, `claude-code`, `pi`, `opencode`
— the four whose config shapes are known and already in use for office-mcp.
LibreChat also needs each surface host in `mcpSettings.allowedDomains`; the
generator emits that too, because it is exactly the step a human forgets.

**Deferred, deliberately named rather than dropped:** `hermes` (Telegram) and
`codex` are listed as consumers in this repo's AGENTS.md but their config shapes
have not been established here. They are additional target strings for the same
generator, not additional architecture — add each once its shape is known.

The new verb must keep `beheaxi conformance "beherouter"` at **6/6**: it needs a
manifest entry with a summary, `mutating = false`, JSON output under `--json`,
and no colour. Conformance is checked in-image, not against a host venv.

## Phasing

| Phase | What | Where | Gated on |
|---|---|---|---|
| **0** | Attach **gitea-mcp** — fresh PAT, `probe = "get_me"`, corrected `GITEA_HOST` | homelab | — |
| **1** | Make `cli` callable; attach **behecheck** | this repo | — |
| **2** | Search: inline descriptions, enriched index, tiered fuzzy | this repo | — |
| **3** | Shared podman network; attach **office-mcp** | homelab | — |
| **4** | `beherouter client-config`; verify against every agent client | this repo | 0 |
| **5** | `behedraw` beheaxi CLI for drawio-export as the wrapper template | new repo | 1, 3 |

**Phase 0 is deliberately zero-code.** Gitea is on another host and already
reachable from the container (verified, 200); it needs only a PAT. It proves
registry → Caddy → env → surface → search → real tool call end-to-end *before*
any refactor. If that chain is broken, everything after is built on sand.

Phases 1, 2 and 3 are mutually independent and may land in any order.

## Acceptance criteria

The goal is met when all of these hold:

1. `beherouter health --deep --json` exits 0 with every attached surface
   reporting a **passing probe** — not merely attached.
2. A `cli`-kind surface executes a real verb end-to-end through `run_tool` and
   returns its result.
3. `search_tools("<typo>")` and `search_tools("<morphological variant>")` both
   return the intended tool, and each result carries its description without a
   `describe_tool` round-trip.
4. **Each in-scope agent client is verified against the live gateway, by an
   agent actually calling a tool** — LibreChat, pi, Claude Code, OpenCode
   (Hermes and Codex deferred as above). Config loading is not sufficient
   evidence; a surface that lists and searches while every call fails is
   precisely how `gitea-home` died.
5. `beherouter client-config <agent>` output is pasted **unmodified** into that
   client and works. Any hand-edit needed is a bug against the marked goal.

## Risks

- **`pids_limit: 256` now has teeth.** Every stdio backend is a persistent
  child and every `cli` call spawns one. ~36× current idle, but it stops being
  theoretical the moment Phase 1 lands. Re-measure when surfaces attach.
- **Blocking subprocess is the likeliest bug in Phase 1**, and its symptom is
  gateway-wide latency rather than a failure on the offending surface — hard to
  attribute after the fact. Assert on it in tests.
- **`search_tools`' schema change is one-way** once clients are wired. Land it
  before Phase 4.
- **Phase 5 creates a repo per wrapped service.** Hold at one (`behedraw`)
  until it proves itself.
- **Two playbooks, always** (`--tags beherouter` *and* `caddy.yml -l service`).
  A registry entry without its Caddy clause is unreachable; an env token
  without one answers to another client's token.

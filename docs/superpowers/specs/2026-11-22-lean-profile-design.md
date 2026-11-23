# Lean listing profile — chat clients get the meta-tools, not the pins

Status: **approved 2026-11-23**. Next: `writing-plans`.

## Intent

LibreChat's MCP tools cost too much context. Measured live 2026-11-22
(`context_cost`, `estimate/json-4.3`, and a raw `tools/list`):

| Surface | Tool definitions | LibreChat `serverInstructions` | Per request |
|---|---|---|---|
| `plane` | **5,945** — 12 pins × ~460 + 409 meta | ~1,010 (hand copy) | **~7k** |
| `office` | **763** — 4 office tools 354 + 409 meta | ~210 (hand copy) | ~1k |

Every one of those tokens is resent on every model call. LibreChat drives a local
27B model on `brain`; Claude Code, the other `/plane/mcp` consumer, has a large
context and benefits from the pins.

**Why beherouter:** context control is the gateway's whole job (README: "a client
pays a handful of tool definitions instead of hundreds"). The fix belongs here,
not in a LibreChat-side workaround or a hand-made second surface (user, 2026-11-22).

## Goals

1. A client can ask for a **lean** listing of any surface: the meta-tools only,
   with no pins. On `plane` that is ~300–400 tokens instead of ~5.9k.
2. The client picks it **explicitly in the URL**: `/<surface>/mcp?profile=lean`.
   With no `profile` parameter, or `profile=full`, the response is byte-identical
   to today's.
3. The lean profile's **`instructions`** carry the recipe for its own listing
   (search → describe → run), so LibreChat can use `serverInstructions: true` and
   the hand-copied blocks in `librechat.yaml` go away.
4. A plugin whose backend **does its own discovery** (office-mcp:
   `discover`/`invoke`) gets no gateway meta-tools **in its lean listing**: lean
   office = its 4 pins. That saves the 409 tokens office pays today for nothing
   (`tokens_saved: -409`). Its full listing is unchanged, because Hermes was
   verified calling office's `search_tools` (`clientconfig.py`).
5. `client-config librechat` emits the lean URL and `serverInstructions: true`.

## Non-goals

- Slimming the **full** profile, or changing what Claude Code, pi, opencode or
  Hermes see.
- Trimming or rewriting backend tool descriptions or schemas.
- Detecting the client automatically (from `clientInfo`, User-Agent and so on).
  This was considered and rejected; see Approaches.
- Per-surface defaults in `registry.toml`. The profile is a client choice.
- Making `client-config librechat` understand the per-user Keycloak header on
  `plane`. That is a separate generator gap.

## Anti-goals: this fails even if it works when…

- …ANY surface's full listing changes, even by a byte (the instructions
  included). This is a **lasting invariant with a regression test** (a recorded
  `initialize` + `tools/list` per test surface), not a one-time check.
- …lean becomes a second attach: a second backend session, a second health
  record, rate limiter, kill switch or metrics series. Lean is a *view* of the
  same surface.
- …a tool name in a published LibreChat agent's allowlist silently stops
  existing (the documented 2026-09-08 failure: a stale allowlist reads exactly
  like a working one).
- …the lean model ends up calling more often than it saves. With the recipe in
  `instructions`, a cold Plane task should cost no more than two extra calls
  (`search_tools`, `describe_tool`) once per tool per chat.

## Constraints

- FastMCP sends `instructions` at `initialize` as a static value per server.
  Per-request instructions would mean patching FastMCP internals
  (`docs/FASTMCP-NOTES.md`).
- The published `tools` array is frozen at attach time, and only the catalogue
  refreshes (`surface.py`, the `published_names` comment). That holds for both
  profiles.
- Identity, authz, gates, the kill switch, audit and metrics must apply to lean
  exactly as to full. Both run through the **one** `CallPipeline`.
- Per-user surfaces (`plane`: bearer passthrough, `aud plane-mcp`) must work
  unchanged under lean. The Caddy `handle /plane/*` matches the path only, so a
  query string passes through.
- Releases follow the global rule: patch bump only (0.2.6 → **0.2.7**).

## Approaches

1. **Two FastMCP apps per surface, chosen by `?profile=`.** ← **picked by the user.**
   `build_surface` builds a *full* and a *lean* `FastMCP` over the same backend,
   `Catalogue` and `CallPipeline`. Each has its own static tool list and its own
   `instructions`. The gateway's `/<surface>/mcp` route sends the request on by
   the query parameter. Lean's instructions fall out naturally, each profile can
   be costed and tested on its own, and full is untouched. The cost is one more
   FastMCP object per surface.
2. **One app plus listing middleware** (like `_GateListing`). Less duplication,
   but per-request `instructions` would mean patching FastMCP's `initialize`
   handling, which is version-fragile, and the two listings would differ only at
   runtime. Rejected.
3. **An auto-generated second registry entry** (`plane~lean`). Very little new
   code, but it doubles the attach, health, metrics, rate limits and kill switch,
   and the two can drift. Rejected; it breaks an anti-goal.

Also rejected: choosing the profile **by `clientInfo.name`**, which is invisible
and lets a renamed client silently fall back to full, and a **per-surface
registry setting**, which gives every client of a surface the same listing.

## Design

### Profiles

- `full` (the default): today's listing. Pins + `search_tools`, `describe_tool`,
  `run_tool`, `context_cost`. Today's `instructions`.
- `lean`: `search_tools`, `describe_tool`, `run_tool` only. **No pins, and no
  `context_cost`** (it is an operator tool; *my call*).
- Unknown values (`?profile=tiny`): **400** with an RFC 9457 body listing the
  valid profiles. Falling back to full would hide a typo (*my call*).
- **Hidden, not forbidden.** In lean a pinned tool is reachable through
  `run_tool(name, args)`. A direct `tools/call` of its name is refused as
  `unknown_tool`, and the message names the `run_tool` call to make instead
  (*my call, revised while planning*: answering an unlisted name directly
  would bypass FastMCP's result conversion). LibreChat can only call listed
  tools anyway.
- `describe_tool` reports `pinned` against the **full** published set in both
  profiles. A tool's pin status is a property of the surface, not of the view.

### Self-discovering plugins

A new additive `PluginSpec` field, e.g. `self_discovering: bool = False`
(name left to the builder). `office_mcp.py` sets it to `True`. With it set, the
**lean** profile publishes the pins and no meta-tools, since the pins *are* the
discovery path. Full is unchanged (Goal 4). `registry-lint` or attach should
refuse a self-discovering plugin whose entry pins nothing, because its lean
listing would be empty. `API_VERSION` does not move (additive, like `maturity`).

### Instructions

- `full`: unchanged (the `instructions_line` cost sentence).
- `lean`: the cost sentence for the lean listing, plus a short **plugin-supplied
  recipe**. A new optional `PluginSpec` text field holds it, e.g. for Plane: the
  three-step path, one worked example (`search_tools("work items")` →
  `describe_tool("workitem")` → `run_tool("workitem", {...})`), the
  Community-Edition notes already in `COMMUNITY_NOTES`, and a line saying that
  arguments shown in instructions can go straight to `run_tool` without
  `describe_tool`. Target: **under 300 tokens** (measure it with the same
  estimator).
- Deployment-specific text (workspace `homelab`, project names) **is not** in
  the plugin. *My call:* an optional registry key `instructions` (string),
  appended to the **lean instructions only** (full stays byte-identical), so a deployment can name its
  workspace without forking a plugin. If the builder finds this unnecessary
  once the recipe exists, drop it and record why.

### Routing

`build_gateway_app` keeps one mount per surface. Inside it, a small ASGI
dispatcher picks the full or the lean app. Both apps' lifespans run (FastMCP
session managers). *My call, so it holds even if a client drops the query
string mid-session:*

- A request **with an `mcp-session-id`** goes to whichever app issued that id.
  The dispatcher records id → profile when it sees the id in an `initialize`
  response, and forgets it on DELETE or when the session expires. A session id
  is therefore never sent to the wrong app, whatever the URL says.
- A request **without** a session id (an `initialize`, or any stateless
  surface) is routed by `?profile=`.
- An unknown session id (for example after a restart) is routed by
  `?profile=` like a sessionless request, and gets FastMCP's normal error from
  that app.

The builder still confirms that LibreChat keeps the query string on its
`initialize` (it builds the transport from the configured URL); that is the
only request where the query string matters.

### Costing and health

- `context_cost` (full only) gains a `profiles` field: `{"full": N, "lean": M}`.
- `/healthz` and `health --deep` are unchanged. There is one surface and one
  probe.
- Metrics: no new series is required. Adding a `profile` label is left to the
  builder; it must not split existing series in a way that breaks the
  dashboards in the homelab `monitor/` repo.

### `client-config librechat`

It emits `url: …/<surface>/mcp?profile=lean` and `serverInstructions: true` for
every surface, self-discovering ones included (lean office drops the
meta-tools). Unit tests cover it.

## Scope and done

Two plans, run in order (*my call*): **(1) beherouter 0.2.7** in this repo,
done when tests and lint pass, the full-listing regression test passes, and the
lean `plane` / `office` costs are measured and recorded; **(2) the homelab
rollout** below, done when the live LibreChat chat test passes. The "≤ 2 extra
calls per tool per chat" budget is an **acceptance check in plan 2**, not a
release blocker for 0.2.7. If it fails, iterate on the recipe wording (a plugin
text change, i.e. another patch).

## Universal skill shape (user, 2026-11-23)

Skills that drive gateway tools are written so that **one text works on every
profile and every client**: lean or full, LibreChat or Claude Code. *The rules
below are my proposal for what that means.*

- Address tools only through the three meta-tools, which exist on every
  non-self-discovering surface in both profiles: `search_tools(words)` →
  `describe_tool(name)` → `run_tool(name, args)`. Never call a pinned tool by
  its direct name.
- Give known-good calls as literal `run_tool("workitem", {"action": "list",
  ...})` payloads, so a model can skip `describe_tool` on the common path.
- Describe intents in the words agents type ("tasks", "sprint"). The search
  aliases already map those words to tools.
- Name no client-specific suffix (`_mcp_plane`) in the body. A client's own
  wrapper adds it. One line may say "your tools may carry a client suffix".
- Keep deployment facts (workspace, projects, Community-Edition limits) in one
  short section, so the same skill shape can be reused for another
  deployment.
- The lean `instructions` recipe the plugin supplies and the skill do not
  duplicate each other. The instructions give the generic path; the skill
  gives the deployment's recipes.

## Rollout (homelab side, after the release)

1. Vendor 0.2.7 into `ur/service/beherouter/`, then deploy
   (`service.yml --tags beherouter`). Measure: lean `plane` cost, `office` cost
   (expect ~354), and a full `plane` listing byte-identical to before (keep a
   `tools/list` + `initialize` capture taken before the deploy).
2. `babylon/web/librechat.yaml`: both URLs as above, `serverInstructions: true`,
   and delete both hand-copied blocks. Fold whatever deployment-specific line
   still matters from `ur/service/plane/skill/SKILL.md` into the registry
   `instructions` key, and record in that SKILL.md that it is no longer
   mirrored into LibreChat.
3. **Mongo: rewrite the agents' `tools` allowlists**, current document and
   `versions[]`, with a backup first, as done on 2026-11-22. Remove every
   `<pin>_mcp_plane` and `context_cost_mcp_plane` / `*_mcp_office` meta name.
   Keep or add `search_tools_mcp_plane`, `describe_tool_mcp_plane`,
   `run_tool_mcp_plane` and the `sys__server__sys_mcp_plane` marker. Agents:
   **Behemotion is the target agent** (user, 2026-11-23). Research Buddy, and
   any other agent the query `db.agents.find({tools: /_mcp_(plane|office)$/})`
   returns, gets the same allowlist cleanup and no further tuning.
4. **Remove the Unibank demo entirely** (user, 2026-11-23: it was built for a
   demo). Back up first, then delete from LibreChat's Mongo the **Unibank
   Agent**, both of its skills (`protocol_to_tasks`, `personal_assistant`) and
   their public `aclentries` rows. Delete `babylon/web/demo-unibank/` from the
   repo (git history keeps it), along with its mentions in
   `babylon/web/CLAUDE.md`. *Plane's UNIB/ORGZ projects and the 8 seeded
   accounts: see open question Q1.*
5. **Rewrite the generic `plane` skill in the universal shape** (below). It is
   the LibreChat skill used by Behemotion, and its source is
   `ur/service/plane/skill/SKILL.md`. Re-push it to Mongo.
6. A real chat test through LibreChat **with the Behemotion agent**: "what
   tasks do I have" and "create a task" on `brain`, plus one office task
   ("convert this URL to PDF"). Read the Langfuse trace for the call path and
   the per-request prompt tokens before and after.
7. Docs: `babylon/web/CLAUDE.md` (the MCP sections, the allowlist warning box),
   `ur/service/CLAUDE.md` § beherouter (the version row is stale: it says 0.2.2
   while 0.2.6 is vendored), and the beherouter `README`/`docs/architecture.md`
   on profiles.

## Left to the builder

- Field and key names; where the dispatcher lives; whether `profile` is a
  metrics label.
- The exact recipe wording, within the token target.
- Whether to drop the registry `instructions` key (see above).

## Plane-side demo data — not in scope

The spec was approved ("proceed", 2026-11-23) without an answer on Plane's UNIB
and ORGZ projects and the 8 DB-inserted employee accounts. Plane deletions
cannot be undone, so they are **left in place** and filed as a follow-up in
Plane `HOMEL`. Only the LibreChat side and the repo directory are removed.

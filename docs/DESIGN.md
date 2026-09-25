# beherouter — design & research record

Captured from the homelab planning session (2026-06-17). This is the reasoning behind the
approach in `../AGENTS.md`. Not a final spec — finalize via `brainstorming`/`writing-plans`.

## Problem

We run several MCP services (Gitea + more soon). Wiring each into every agent
(LibreChat, Hermes, Mac OpenCode/pi, external coding agents) means either (a) a bespoke wrapper
per service, or (b) dumping hundreds of tool schemas into every client's context. The hand-written
wrappers we already run prove the pain: each hardcodes a static allowlist — of the order of 9 tools
out of a backend's 109 — which works, but it is per-service bespoke code and the trimmed tools are
unreachable.

**Goal:** one VM-hosted gateway; each service a separate MCP surface; attachable to many agents
*without* context bloat; easy to add services.

## Chosen approach — B: hybrid pinned + tool-search, built on FastMCP

Per surface: a small **pinned** flat-tool set (common verbs, like today's hand-picked allowlists) **plus**
`search_tools` / `describe_tool` / `run_tool` meta-tools for the long tail. Small local models get
the easy direct path; capable agents reach everything on demand. Context = pinned set + 3 meta-tools,
regardless of backend size. This mirrors what Claude Code itself does (core tools + ToolSearch).

Alternatives considered:
- **A — pure tool-search facade** (only search/describe/run): minimal context but every action is a
  3-call dance and nested `run_tool` args are hard for the small family models. Rejected as default.
- **C — adopt an off-the-shelf aggregator**: see landscape + spike below. Rejected — none give
  embeddings-free *dynamic* search, and they'd strand the per-backend middleware we already run.

## Research: how MCP aggregators work

All are the same shape: act as an MCP **client** to N backends, re-expose as one/few MCP **server**
surfaces. They differ on 4 axes = our 4 requirements: surface shape (merged vs per-service),
credential model (static vs **per-user pass-through** — which must carry **more than one header per
backend**, since a backend's auth may be a PAT *plus* a workspace/tenant header), context control
(dump vs **filter/search**), management (config vs UI).

Landscape (mid-2026), filtered to our needs:

| Project | Per-user creds | Context control | Per-service endpoints | Note |
|---------|:---:|---|:---:|------|
| **LiteLLM MCP** | ⚠️ 1 header | static allowlist **or** *semantic (embeddings)* | ✅ | already running on gpu-service; see spike |
| **MCPHub** (TS) | ✅ header fwd | vector search (**embeddings**) | ✅ | nice web UI |
| **IBM ContextForge** (Py) | ✅ `X-Upstream-Authorization` | static virtual-servers (no search yet) | ✅ | heavyweight, RBAC/UI |
| **FastMCP** (lib) | ✅ arbitrary per-session | **BM25 / regex (no embeddings)** + transforms | ✅ via mount | our pick; the wrappers we already run use it |
| MetaMCP, mcp-proxy (×2), TBXark, MS gateway, MCP Router | mostly ❌ static | static filter only | mixed | — |

**The embeddings fault line:** polished products that do *dynamic* tool-search rank with embeddings
(removed from this homelab, 2026-04-20). Embeddings-free **BM25** dynamic search is mainly a *library*
feature — and FastMCP ships it natively. That single constraint pushes us to build on FastMCP.

## Research: how systems fight tool/context bloat

- **MCP Tool Search** (`defer_loading: true` + bm25/regex search → auto-expanded
  `tool_reference`s; prefix unchanged so prompt cache survives): **~85% token cut, MCP-eval accuracy
  49% → 74%** — fewer tools in context makes the model *more* accurate, not just cheaper.
- **Code execution with MCP** (tools as a code API, discovered on demand): ~98% token reduction.
- **MCP spec supports dynamic tools**: `tools.listChanged` capability + `notifications/tools/list_changed`,
  plus cursor pagination — so a server can change its advertised tools mid-session.
- **"Tool search as a tool"** (`search_tools`/`run_tool`) is mainstream (Docker `mcp-find`, Lasso 2-tool
  dispatch, 1MCP progressive, MCPProxy `retrieve_tools`). Ranking = BM25 / embeddings / hybrid.
- **BM25 caveat:** lexical accuracy drops past a few hundred tools (Stacklok: 34% BM25 vs 94% hybrid on
  ~2,800 tools). **Not a problem here** — per-surface scale is ~100, and we keep search scoped per service.
- Embeddings-free BM25 libs available: FastMCP transforms, SQLite FTS5, Bleve, bm25s/rank-bm25.

## Spike: LiteLLM-MCP (running 1.82.6 on gpu-service) — REJECTED

| Requirement | Result |
|---|---|
| stdio + http backends | ✅ stdio (PR #12530) → `gitea-mcp stdio`; http → front an existing FastMCP wrapper |
| per-service endpoints (`/<server>/mcp`, `x-mcp-servers`) | ✅ |
| client compat (streamable-http/SSE, LibreChat) | ✅ |
| per-user creds | ⚠️ **1 header/backend** (`x-mcp-<server>-authorization`); a backend whose auth is a PAT **+** a workspace/tenant header therefore cannot be served; multi-header is open issue #12895 |
| **embeddings-free dynamic search** | ❌ **static allowlist (= today) OR semantic filter that needs an embedding model** |
| OSS vs Enterprise | ⚠️ core gateway OSS; fine-grained permissioning + Admin UI are **Enterprise** (~$250/mo) |

**Verdict:** fails the defining axis (embeddings-free dynamic search) and the two-header case.
Build on FastMCP instead. (LiteLLM stays our LLM gateway; it could still be a unified front door later
if we ever accept Enterprise + embeddings — not now.)

## Open questions for the build session

- Surface URLs: one host `mcp.example.com/<service>/mcp`, or per-service DNS names?
- Pinned set per service (Gitea especially — pick the common ~8–10 verbs).
- `run_tool` ergonomics for small models (the family chat agents) — measure vs today's flat tools.
- Whether a backend with its own middleware is mounted as an HTTP backend (keeping that middleware)
  or re-folded into beherouter.
- Per-user PAT plumbing through FastMCP per-session client factory for each backend.
- Auth to the gateway itself + the LibreChat "anonymous probe → 200" handling per surface.

## Search design (2026-09-25)

Spec: [`docs/superpowers/specs/2026-09-25-tool-search-quality-design.md`](superpowers/specs/2026-09-25-tool-search-quality-design.md).
The index is our own (`search.py`, `rank_bm25` + `rapidfuzz`), not FastMCP's
`BM25SearchTransform`.

**The measured problem.** Against the real `tools/list` of `plane-mcp-server`
0.3.2 (30 tools), one `search_tools` call cost ≈ 3 200–4 300 tokens: up to ten
hits, each carrying the full ~1 000-character description plus annotations. One
or two searches spent the whole saving that pinning 11 of 30 tools bought. The
index was also wrong for MCP tools: `_arg_tokens` iterated a JSON Schema's top
level, so `properties required type additionalProperties` were indexed into
every tool and the real argument names and enums never were. Filler words
("to", "of", "a") put most of the corpus in the first tier, so "workitems"
missed `workitem`.

**What is indexed.** One BM25 document per tool, fields weighted by repetition:
name tokens plus the joined name ×3, `search_aliases` ×2, the description's first
sentence ×2, the rest ×1, argument names/enums/descriptions ×1 (dialect-aware:
JSON Schema `properties`, or a beheaxi manifest). Index and query share one
`normalize`: camelCase split, a short fixed stopword list, rule-based plural
folding. Nothing is downloaded at runtime.

**Scoring.** Each query token earns its best match per tool: exact ×1.0,
prefix ×0.6, fuzzy (`ratio ≥ 85`) ×0.4. Expansion always runs, not only as an
under-fill fallback, but an expansion is capped at its weight times the token's
best *exact* score, because a rare misspelling carries a far higher IDF than the
common word the agent typed. A query equal to a tool's name ranks that tool
first. **BM25Plus, not Okapi**: Okapi's IDF is ≤ 0 for a term in half the corpus,
and `project`/`create`/`list`/`page` are in 23–29 of Plane's 30 tools, so
"create a project" never found `project`.

**Cutoff.** Default `limit` 5. A hit must score ≥ 35 % of the best, *and* match
≥ 60 % of the distinct query tokens the best surviving hit matches. The second
rule is not in the spec. It was added during tuning because "send an email"
matched `email`, a real field on five Plane tools, equally well on all five, and
no score threshold separates equally weak hits.

**Hit shape.** `{name, brief, mutating?, pinned?}`: `brief` is the first
sentence, capped at 160 characters. The full description and schema are
`describe_tool`'s job. `search_tools` and `beherouter search` share one builder
(`indexing.search_hits`).

**Arguments.** Pinned tools and `run_tool` share `args.prepare_args`: both
spellings accepted (`--flag-name` / `flag_name`), `None` and schema-default
echoes dropped (the `archive=True`-on-`create` failure class, previously open on
`run_tool`), a missing required arg refused with its enum, an undeclared arg
refused with a suggestion on a *closed* schema only. Types and enum membership
are deliberately not checked. Unknown tool names are `NotFound` with "did you
mean".

**Measured after** (`tests/test_search_eval.py`, 40 queries + 4 negatives):
top-1 32/40 (was 10/16 on the original 16), top-3 39/40, 2.9 hits per query,
largest search ≈ 126 tokens, every negative ≤ 1 hit.

**Rejected.** *Per-field BM25 (BM25F)* is the principled weighting, but it means
more code and more tunables, and repetition-weighting reached the gates.
*FastMCP's `BM25SearchTransform`* is single-field BM25 with no normalisation,
weighting or vocabulary, so adopting it would lose every gain above.
*Embeddings* are a hard constraint.

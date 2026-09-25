# Tool search — reliable, cheap, precise — design

**Date:** 2026-09-25
**Status:** design approved in chat; **nothing implemented.**
**Scope:** the four meta-tools' search half (`search_tools`, `describe_tool`,
`run_tool`) and the index behind them. `context_cost` is unchanged.
**Release:** patch, `0.2.3 → 0.2.4`.

## The motto this serves

> **To agents:** a thin, curated MCP surface — a short pinned tool list plus fuzzy
> search that locates the long tail on demand, so a client pays a handful of tool
> definitions instead of hundreds.

The pinned half of that promise is kept. The search half is not, and it was
measured, not suspected.

## Problem — measured 2026-09-25

Method: the real `tools/list` of `plane-mcp-server==0.3.2` (30 tools, recorded
over stdio — no network) run through today's `build_index` with 16 agent-style
queries.

### 1. A search costs more context than it saves

Plane's descriptions average ~1 000 chars (~240 tokens). `search_tools` returns
up to 10 hits by default, each with the **full** description plus
`annotations`:

| Query | `search_tools` response today |
|---|---|
| "add a comment to an issue" | ≈ 4 290 tokens (10 hits) |
| "change status of a ticket to done" | ≈ 3 190 tokens (10 hits) |
| "who is on the project team" | ≈ 3 940 tokens (10 hits) |

The same hits as name + first line cost ≈ 70–80 tokens. `describe_tool(workitem)`
is ≈ 1 270 tokens, half of it the description the search already returned.
Search results stay in the conversation, so **one or two searches spend the whole
saving that pinning 11 of 30 tools was meant to buy.**

### 2. MCP backends index JSON-Schema keywords, not their arguments

`indexing._arg_tokens` was written for the beheaxi dialect
(`{arg: {name,type,required,enum}}`). An MCP descriptor's `schema` is JSON
Schema, so it iterates the top-level keys and indexes
`additionalProperties properties required type` into **every** tool — while the
real argument names and enum values (Plane's `action`: `list`, `create`,
`archive`, …) are never indexed.

### 3. Tier-gating lets filler words decide membership

`ToolIndex.search` puts a tool in tier 0 on **any** single-token overlap, and
only consults prefix/fuzzy tiers when tier 0 under-fills `limit`. There is no
stopword list, so "a", "to", "of" fill tier 0 with most of the corpus:

- "change status of a ticket to done" → 10 hits, `state` absent.
- "workitems" → `workitem` absent: seven descriptions contain the literal token
  `workitems`, tier 0 fills, and the prefix tier that would find `workitem`
  never runs.

### 4. No normalisation, no vocabulary, no confidence

No plural folding (`issues` ≠ `issue`), no camelCase split, no synonyms
("sprint" → 0 hits; Plane calls it `cycle`), and no cutoff — a vague query
always returns a full page.

### 5. `run_tool` and `describe_tool` are the loose end

- `run_tool` skips the arg normalisation pinned tools get (`surface.py`
  `_make_pinned_tool`): `None` values and schema-default echoes are forwarded.
  That is the `archive=True`-on-`create` failure class, still open on the
  long-tail path.
- An unknown name returns `{"error": ...}` as a **successful** result — not an
  MCP tool error — with no suggestion.
- `describe_tool` always emits `"callable": true`.

### Baseline

| Metric | Today |
|---|---|
| expected tool at rank 1 | 10/16 (63 %) |
| expected tool in top 3 | 13/16 (81 %) |
| mean hits per query | ~8 |
| largest `search_tools` response | ≈ 4 300 tokens |

A throwaway prototype of §1 below (no aliases) reached 11/16 top-1 at 3.5 mean
hits; with a six-line Plane vocabulary, **12/16 top-1, 15/16 top-3, 3.5 mean
hits.**

### Doc drift found on the way

AGENTS.md § The one-paragraph architecture says search is FastMCP's
`BM25SearchTransform`. It is not — it is our own `rank_bm25` + `rapidfuzz`
index in `search.py` (FASTMCP-NOTES.md already records that the plan replaced
it). FastMCP 3.4.5's transform is plain single-field BM25 with no
normalisation, weighting or vocabulary, so adopting it would lose every gain
below; it is rejected.

## Approach

**Rework the existing `ToolIndex` in place.** Same dependencies (`rank_bm25`,
`rapidfuzz`), same public interface, one file. Rejected alternatives:

- **Per-field BM25 (BM25F-style).** The principled way to weight fields, but more
  code and more tunables; at 30–100 tools per surface, repetition-weighting a
  single document is indistinguishable.
- **FastMCP's `BM25SearchTransform` plus our own pre-processing.** Fights a fixed
  tokenizer; see above.

**No embeddings** — the AGENTS.md hard constraint holds throughout.

## Design

### 1. Index and scoring — `search.py`, `indexing.py`

#### What is indexed

One BM25 document per tool, fields weighted by repetition:

| Field | Weight | Source |
|---|---|---|
| name tokens + the name de-underscored (`workitem_comment` → `workitem comment workitemcomment`) | ×3 | `ToolDescriptor.name` |
| search aliases (§4) | ×2 | `PluginSpec.search_aliases` ∪ registry additions |
| first sentence of the description | ×2 | `summary` |
| rest of the description | ×1 | `summary` |
| arg names, enum values, arg descriptions | ×1 | `schema`, dialect-aware |

**Dialect-aware `_arg_tokens`.** Detect the dialect by the same marker keys
`surface._normalize_args` uses (`properties` / `$schema` / `type == "object"`).
JSON Schema: walk `properties` — the name, `enum`, enums nested in
`anyOf`/`oneOf`, and `description`. beheaxi: today's behaviour (name + enum).
JSON-Schema keywords are never indexed.

#### Normalisation

One `normalize(text) -> list[str]`, used for BOTH index and query:

1. split camelCase (`projectId` → `project id`), lowercase;
2. tokenise on `[^a-z0-9]`;
3. drop a small fixed English stopword list (module constant);
4. fold plurals by rule, only for tokens of ≥ 5 chars: `-ies → -y`, `-es → ''`
   after `s/x/z/ch/sh`, else `-s → ''`; never strip a `-ss` ending, and never
   touch a small protected set (`status`, `alias`, `access`, `address`, …).

Rule-based by design — no NLTK, nothing downloaded at runtime (the same reason
`costing.py` estimates rather than tokenises).

#### Scoring

Each query token contributes its BM25 score, floored at 0, times a match weight:

| Match | Weight | Condition |
|---|---|---|
| exact | 1.0 | — |
| prefix | 0.6 | both tokens ≥ 4 chars, one is a prefix of the other |
| fuzzy | 0.4 | `rapidfuzz.fuzz.ratio ≥ 85`, both tokens ≥ 5 chars |

Expansion runs over the index vocabulary, **always** — not as an under-fill
fallback. **Exact-name rule:** a query whose normalised form equals a tool's
normalised name (either spelling) ranks that tool first regardless of score.

#### Cutoff

- `limit` default drops **10 → 5**.
- Keep only hits scoring ≥ 35 % of the top score.
- A tie is never split: every hit equal to the last kept score is kept if within
  `limit`. `test_term_present_in_half_the_corpus_is_still_found` (50 of 100
  equal hits) stays green unmodified.
- No hit → `[]`, as today.

All weights and thresholds are named module constants, each with a comment,
tuned against the evaluation set (§5), not by eye.

### 2. Meta-tool response shapes — `surface.py`, `cli/app.py`

#### `search_tools(query, limit=5)`

```json
[{"name": "workitem_comment", "brief": "Comments on a work item.", "mutating": true}]
```

- `brief` = first sentence of the description, capped at 160 chars with `…`.
  Computed once per catalogue build, not per call.
- `mutating` appears only when not `None`; `pinned: true` only when true (it tells
  the agent to call directly rather than through `run_tool`). Raw `annotations`
  leave the search result.
- The field is renamed `summary` → `brief` deliberately: it no longer carries
  the full description and must not be mistaken for it.
- Tool description becomes: *"Search this surface's tools. Returns name + one-line
  brief; call describe_tool for arguments before run_tool."*

**One hit builder.** The CLI `beherouter search` verb (`cli/app.py:256`) today
builds its own hits with the full `summary`. Both it and `search_tools` call one
`search_hits(catalogue_or_descriptors, query, limit)` so the two cannot diverge —
the same reason `indexing.build_index` exists.

#### `describe_tool(name)`

- Keeps the full description and `args` (the backend's schema, unaltered).
- Drops `"callable": true`; omits `annotations` and `returns` when empty.
- Unknown name → raises **`NotFound`** (MCP tool error) with up to three
  suggestions: fuzzy over catalogue names, falling back to the search index.

#### `run_tool(name, args)`

- Unknown name → the same `NotFound` with suggestions.
- Arg problems → `UsageError` (§3).
- Success payload unchanged.

#### Compatibility

All five consumers are LLMs reading the JSON; none parses it programmatically,
so a renamed field breaks nothing mechanical. The CHANGELOG states the shape
change anyway. Tests asserting the old shape (`test_surface.py:59`, `:75`;
`test_cli_app.py:233`) are updated, not deleted.

### 3. `run_tool` hardening — `surface.py`

#### One normalisation path

Extract `prepare_args(descriptor, args) -> dict` from `_make_pinned_tool`; both
pinned tools and `run_tool` call it. In order:

1. **Name mapping.** Accept the backend's wire name (`--flag-name`, what
   `describe_tool` shows) *and* the sanitised param name (`flag_name`, what
   pinned tools publish); forward the wire name. Both spellings of one arg →
   `UsageError`.
2. **Drop unset.** `None` values and values equal to the schema's own default
   are dropped — today's rule and its `archive=True` rationale, now on the long
   tail too.
3. **Validate.**
   - Missing required arg → `UsageError`, listing enum values when present:
     *"workitem: missing required arg 'action' (one of: list, create, update, …)"*.
   - Undeclared arg → `UsageError` with a fuzzy suggestion
     (*"unknown arg 'projectId'; did you mean 'project_id'?"*) — **only when the
     schema is closed**: JSON Schema with `additionalProperties: false`, or any
     beheaxi manifest (its arg set is complete by definition). An open schema
     forwards unknown args untouched. All 30 Plane schemas are closed.

**Not checked:** types, enum membership, nesting. Full JSON-Schema validation
was considered and rejected — it adds a dependency and refuses calls a backend
whose schema is stricter than its behaviour would accept.

#### Order on the call path

`guard()` → `ensure_fresh()` → lookup (`NotFound`) → `prepare_args`
(`UsageError`) → `dispatch`. Identity and backing guards (Plane edition
refusals) run after, unchanged. A refused caller learns nothing about args.

#### Pinned tools

Their synthesized signatures already make FastMCP reject malformed calls before
`prepare_args`; the change there is only that the shared code moved. **Their
published schemas must stay byte-identical** — a test pins it, because a
changed published `tools` array invalidates a host's prompt cache.

### 4. Plugin search vocabulary — `search_aliases`

#### Declaration

```python
# plugins/spec.py — PluginSpec
search_aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
```

Plane's lives once in `plugins/plane.py` and is shared by `plane`,
`plane-http-apikey` and `plane-http` exactly as `PINNED`/`PROBE` are:

```python
SEARCH_ALIASES = {
    "workitem": ("issue", "ticket", "task", "epic", "bug", "story"),
    "cycle":    ("sprint", "iteration"),
    "state":    ("status", "workflow", "column"),
    "member":   ("user", "people", "team", "assignee"),
    "intake":   ("triage", "inbox"),
    "work_log": ("timesheet", "hours", "time tracking"),
    # final list tuned against the evaluation set
}
```

`office-mcp`, `sonarqube`, `gcal`, `m365` start empty.

**The rule for adding a word:** it is a word an agent would type that the
backend's own description does not contain. Documented in `docs/PLUGINS.md`
§ Search vocabulary.

#### Registry additions

```toml
[plane.search_aliases]
cycle = ["sprint", "PI"]   # UNION with the plugin's words, never replaces
```

Additive-only, so the tested vocabulary cannot be lost by accident — the same
principle as `pinned`/`probe` being overrides of a tested default.

#### Plumbing

- `registry.py`: `RegistryEntry.search_aliases: dict[str, list[str]] | None`,
  shape-validated alongside `pinned` (a table of string arrays).
- `models.Backend.search_aliases: dict[str, tuple[str, ...]]`, set in
  `gateway.py` where `ttl_ms` is merged today (spec ∪ entry). No plugin `build`
  changes.
- `indexing.build_index(descriptors, aliases=None)` — still the single place
  descriptors become index entries. `Catalogue` passes `backend.search_aliases`
  on every rebuild, so vocabulary survives a refresh; the CLI `search` verb
  passes it too.

#### Guards

- `registry-lint`: **error** on a malformed value; **warning** when an alias
  names a tool the plugin neither pins nor lists in its own
  `search_aliases` (lint performs no attach, so the full catalogue is unknown).
- `health --deep`: **warning**, not failure, when an alias names a tool the
  backend does not serve — beside the existing `pinned_missing` check.
- Plugin test: every key in a plugin's own `SEARCH_ALIASES` is a tool in its
  recorded catalogue snapshot (§5).

### 5. Evaluation, tests, docs

#### Evaluation set — `tests/search_eval/`

- `catalogues/plane-0.3.2.json` — the real `tools/list` snapshot (name,
  description, inputSchema, annotations), recorded over stdio.
- `catalogues/fake-cli.json` — generated from `tests/fixtures/fake_tool.py`, to
  keep the beheaxi dialect covered.
- `queries/plane.toml` — ~40 queries with an expected tool each: the 16 from the
  measurement, plus synonyms, plurals, typos, stopword-heavy questions,
  `work item`/`workitem` splits, and **negative** queries (e.g. "send an email")
  whose expectation is `[]` or at most one weak hit.
- `scripts/record_catalogue.py` — re-records a snapshot, beside
  `scripts/calibrate_tokens.py`. AGENTS.md's re-probe-after-upgrade rule gains
  "re-record the snapshot and re-run the evaluation set".

#### Gates — `tests/test_search_eval.py`, plain pytest

| Metric | Gate | Baseline |
|---|---|---|
| top-3 hit rate | ≥ 90 % | 81 % |
| top-1 hit rate | ≥ 75 % | 63 % |
| mean hits per query | ≤ 4 | ~8 |
| negative queries with ≤ 1 hit | 100 % | 0 % |
| largest `search_tools` payload over the set (`costing.estimate_tokens`) | ≤ 400 tokens | ≈ 4 300 |

Final thresholds are set just under what the finished scorer measures — tight
enough to catch a regression, not so loose that one hides. A failure prints the
full query → expected → got table.

#### Unit tests (red first)

- `normalize`: camelCase, stopwords, plural rules, protected words.
- `_arg_tokens` on both dialects; regression: `properties` / `required` /
  `type` / `additionalProperties` are never indexed for a JSON-Schema tool.
- exact-name rule; relative cutoff never splits a tie.
- `prepare_args`: name mapping, both-spellings refusal, default-echo drop,
  missing-required, unknown-arg on closed vs open schema.
- `NotFound` suggestions from `describe_tool` and `run_tool`.
- pinned tools' published schemas byte-identical before/after.
- aliases: additive merge, survive a `Catalogue` refresh, lint error/warning,
  health warning.
- `search_hits` is the single builder: CLI and MCP produce the same hits.

#### e2e

One step added to `tests/e2e/e2e.py` against the real plane-mcp-server:
`search_tools("sprint")` → `cycle`; `run_tool` with a misspelled arg →
`UsageError` carrying a suggestion.

#### Docs

- AGENTS.md: correct the `BM25SearchTransform` claim; § Mission reflects the
  brief-hit shape.
- `docs/DESIGN.md`: a § Search design section (fields, weights, cutoff, why no
  BM25F).
- `docs/PLUGINS.md`: § Search vocabulary.
- `CHANGELOG.md` `## [0.2.4]`: the `search_tools` shape change, the new
  `NotFound`/`UsageError` behaviour, `search_aliases`.
- `HARNESS-DIVERGENCES.md`: remove any entry this closes.

## Out of scope

- Embeddings (hard constraint).
- Suppressing the search meta-tools on fully-pinned surfaces (`office`).
- Type/enum/nested argument validation.
- Evaluation sets for `office-mcp` and `sonarqube` — both need a live server
  this workstation cannot reach from Python; a follow-up.
- Re-vendoring into the homelab repo — the usual separate delivery step.

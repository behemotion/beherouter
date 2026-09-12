# beheaxi — Layer-1 Design (shared CLI / AXI framework)

**Date:** 2026-06-21 · **Status:** Approved (design), pending spec-review + implementation plan
**Conforms to:** `2026-06-21-harness-foundation-design.md` (Layer-0 contract — source of truth)
**Repo home:** This spec is authored in `beherouter/docs/superpowers/specs/` because the `beheaxi`
repo does not exist yet. **First implementation step** is to create the `beheaxi` repo and move
this spec into `beheaxi/docs/superpowers/specs/`.

---

## 1. Purpose

beheaxi is the shared Python library every BEHEMOTION tool's CLI is built on, so the AXI standard
is enforced **by construction** rather than re-implemented (and re-diverged) per tool. It is the
first thing built; everything else depends on it (foundation spec §8).

It provides, to each tool:

- A CLI framework with shared command wiring and standard global flags (`--json`, `--quiet`,
  `--no-color`).
- A no-arg **live dashboard** rendered uniformly across all tools from a small per-tool hook.
- **Token-efficient** machine output (one JSON document per command in `--json` mode).
- A structured **error envelope** (problem+json-style) and **categorized exit codes**.
- **`<tool> describe --json`** — the registration manifest beherouter consumes, generated from the
  Typer command tree so it can never drift from the actual CLI.
- A **black-box conformance runner** that verifies AXI-compliance against the shipped binary.

beheaxi is a **library first** plus a thin CLI (the conformance runner) that dogfoods the standard.
It is **not a deployed service**: no `deploy.md`, no port, no database. It is a build-time
dependency.

## 2. Locked decisions (from brainstorming, 2026-06-21)

1. **Framework base: Typer over Click.** Type-hint-driven commands let the `describe --json`
   manifest be introspected directly from function signatures (drift-proof by construction). Typer
   is built on Click, so behelib's existing raw-Click commands can be mounted and migrated
   incrementally.
2. **Dashboard: framework renders, tool supplies a `status()` hook.** beheaxi owns the layout so
   every tool's no-arg view is identical for free; each tool implements only a small hook returning
   `{state, suggested_next_commands}`.
3. **Conformance: black-box subprocess runner.** `beheaxi conformance <tool-cmd>` invokes the real
   shipped binary as a subprocess and asserts the contract — the same surface beherouter uses, and
   language-agnostic for any future non-Python tool.
4. **Dependency model: versioned git dependency.** Each tool pins
   `beheaxi @ git+https://github.com/behemotion/beheaxi@vX.Y.Z`; repos stay independently buildable.
   A local-path `[tool.uv.sources]` override supports local co-development without shipping.
5. **Public API shape: one `BeheaxiApp` object** (hybrid — blessed object that auto-wires the
   standard, with internals exported as escape hatches). A tool cannot be non-compliant by accident.

## 3. Scope

**v1 (in):** the `BeheaxiApp` object; global flags + `AxiContext`; output `emit()` (JSON/Rich);
error envelope + `ExitCode`; `describe` generation + JSON Schema; dashboard from the `status()`
hook; the conformance runner (structural checks).

**Deferred (YAGNI):** pytest fixtures / in-process assertion helpers; config-file loading; a logging
framework; shell-completion scripts; a plugin system; i18n/localization.

## 4. Package layout

```
beheaxi/
  pyproject.toml              # name=beheaxi; requires-python>=3.12; build=hatchling
                              # [project.scripts] beheaxi = "beheaxi.cli:main"  (a callable, not the app object)
                              # deps: typer, rich, jsonschema (conformance only)
  src/beheaxi/
    __init__.py               # public API: BeheaxiApp, Status, AxiError (+ subclasses), ExitCode
    app.py                    # BeheaxiApp — wires global flags, describe, dashboard, error handler
    context.py                # AxiContext (json/quiet/no_color + output mode) on ctx.obj
    output.py                 # emit(data): JSON→stdout in --json mode, else Rich render; table/panel helpers
    errors.py                 # AxiError hierarchy, problem+json envelope, ExitCode enum
    describe.py               # introspect command tree → manifest dict; the `describe` command impl
    manifest_schema.json      # the describe --json contract (JSON Schema)
    dashboard.py              # Status dataclass + uniform renderer
    cli.py                    # beheaxi's OWN cli (conformance, version) — built on BeheaxiApp (dogfood)
    conformance/
      __init__.py
      runner.py               # subprocess black-box checks
      checks.py               # individual check functions
  tests/                      # unit tests + schema fixtures + self-conformance
```

Baseline (from repo audit): Python ≥3.12 (behelib at 3.13), `uv` + `hatchling`, consistent with
beherouter/behelib.

## 5. The public API — `BeheaxiApp`

```python
from beheaxi import BeheaxiApp, Status

app = BeheaxiApp(name="behelib", version="1.0.0",
                 summary="Knowledge layer — agentic + graph RAG.")

@app.command(pinned=True, mutating=False)        # records pinned/mutating into the manifest
def search(query: str, shelf: str = None):
    """Ranked semantic+graph search over indexed knowledge."""
    app.emit(results)                            # JSON in --json mode, Rich table otherwise

@app.status()                                    # the dashboard hook
def status() -> Status:
    return Status(state={"shelves": 3, "indexed_docs": 1240},
                  suggest=["behelib search <query>", "behelib fill <box>"])
```

Constructing `BeheaxiApp` auto-wires, at construction time:

- the global-flag callback (`--json` / `--quiet` / `--no-color`), storing an `AxiContext` on
  `ctx.obj`;
- the `describe` command (emits the manifest; `describe --json` is the contract surface);
- the no-arg dashboard (invokes the registered `status()` hook + manifest);
- a top-level error handler that catches `AxiError` and uncaught exceptions, renders the envelope,
  and exits with the categorized code.

`@app.command(pinned=..., mutating=...)` wraps Typer's command registration and records the two
metadata bits alongside it. Internals (the flag callback, the describe generator, the dashboard
renderer) are also exported for rare escape hatches.

## 6. `describe --json` — the keystone contract

Generated by introspecting the Typer command tree; never hand-maintained. Each invocable **leaf**
command → one verb. Nested command groups become space-joined names (`shelf create`), which beherouter
maps to flat MCP tools (`behelib_shelf_create`).

**Type mapping** (Python → schema string): `str→string`, `int→integer`, `float→number`,
`bool→boolean`, `Path→string`, `Enum→string` (+`enum` values), `list[T]→array`. `required` = the
parameter has no default.

```json
{
  "tool": "behelib",
  "version": "1.0.0",
  "summary": "Knowledge layer — agentic + graph RAG over local docs.",
  "verbs": [
    {
      "name": "search",
      "summary": "Ranked semantic+graph search over indexed knowledge.",
      "args": [
        {"name": "query",   "type": "string", "required": true},
        {"name": "--shelf", "type": "string", "required": false}
      ],
      "pinned": true,
      "mutating": false
    }
  ]
}
```

`pinned` marks the common verbs beherouter exposes as flat MCP tools; the rest are reachable via
`search_tools` / `describe_tool` / `run_tool`. `mutating` lets beherouter/agents distinguish read from
write. The output validates against `manifest_schema.json`; the conformance runner asserts that
validation, so the schema is the enforced contract beherouter builds against.

## 7. Output, errors, exit codes

**Output contract.** In `--json` mode, every command emits exactly **one JSON document to stdout**
(object or array), so a consumer can always `json.parse(stdout)` on success. Human mode renders Rich.
`--quiet` suppresses non-essential chrome; `--no-color` strips ANSI (and is auto-on for non-TTY).
The framework helper `app.emit(data)` serializes-or-renders based on the active `AxiContext`.

**Error envelope** (problem+json-style). In `--json` mode the envelope is emitted as JSON to
**stderr** (stdout stays clean for success payloads); in human mode a friendly Rich message goes to
stderr. The exit code is the primary machine signal.

```json
{
  "error": {
    "type": "not_found",
    "title": "Shelf not found",
    "detail": "No shelf named 'foo'. Try `behelib shelf list`.",
    "code": 3,
    "context": {"shelf": "foo"}
  }
}
```

**Exit-code classes** (`ExitCode` enum), aligned with Typer/Click's baked-in `2 = usage`:

| Code | Class | Meaning |
|---|---|---|
| 0 | ok | success |
| 1 | internal | unexpected / uncaught error |
| 2 | usage | bad arguments or flags (Click default) |
| 3 | not-found | requested resource does not exist |
| 4 | auth | authentication / authorization failure |
| 5 | conflict | already-exists / state conflict |
| 6 | unavailable | backend / dependency down |

`AxiError` subclasses (`UsageError`, `NotFound`, `AuthError`, `Conflict`, `Unavailable`) carry their
code and envelope fields. The top-level handler maps any uncaught exception to `internal` (1).

**Canonical source.** This `ExitCode` enum lives in beheaxi and is imported by every tool — it is
the one authoritative definition, so no tool re-derives its own scheme. It extends the five named
classes in foundation §3 (`usage`/`not-found`/`auth`/`conflict`/`internal`) with `unavailable` (6)
for backend/dependency outages; the foundation's "distinct non-zero classes" wording permits this.

**Flag placement.** The global flags MUST be accepted both **before and after** the subcommand —
`behelib --json describe` and `behelib describe --json` are equivalent. (Typer/Click flag placement
is order-sensitive by default; the framework wires the global callback so either arrangement works,
and the conformance runner exercises both.)

## 8. Dashboard

No-arg invocation renders a uniform view from the manifest + the `status()` hook:

1. header — tool name + version + summary;
2. state table — the hook's `state` dict;
3. "Next:" — the hook's suggested commands;
4. verb menu — grouped, pinned verbs highlighted.

One renderer in beheaxi ⇒ identical UX across all tools. A tool that registers no `status()` hook
still gets header + verb menu (state/suggest sections omitted).

## 9. Conformance runner

`beheaxi conformance <tool-cmd>` runs the tool's real binary as a subprocess and asserts:

1. `describe --json` exits 0 and validates against `manifest_schema.json`;
2. every `pinned` verb is present, each with a summary and args;
3. `<tool> --json describe` is parseable JSON; `<tool> --no-color …` output contains zero ANSI;
4. no-arg invocation exits 0 and prints non-empty output (the dashboard);
5. a bogus flag/command exits 2 (usage).

**Optional deeper checks:** a tool may ship a `conformance.scenarios.toml` mapping specific
command invocations to expected exit codes, letting the runner verify the not-found/auth/conflict/
unavailable categories per tool. Absent the file, the structural checks above are the gate.

The runner exits 0 (all pass) or non-zero (with a per-check PASS/FAIL report).

## 10. Distribution, deploy, testing

- **Dependency:** versioned git dep (`beheaxi @ git+https://github.com/behemotion/beheaxi@v0.1.0`)
  + a local-path `[tool.uv.sources]` dev override (not shipped).
- **No deploy surface:** beheaxi has no `deploy.md`, no port, no Postgres — it is a build-time
  library, not a service. (It is therefore absent from the deploy port registry.)
- **GitHub:** `gh repo create behemotion/beheaxi --private` (deferred to the implementation phase;
  outward-facing, confirm before first push).
- **CI:** `ruff` + `mypy` + `pytest`, plus beheaxi runs **its own conformance runner against its own
  CLI** — dogfooding proves the framework passes the standard it defines.
- **Release:** tag `v0.1.0` once green; that tag is what behelib (the first consumer, foundation
  spec §8 step 3) pins.

## 11. Open items / deferred to the implementation plan

- Exact Typer introspection mechanics for nested groups and `Annotated[...]` parameter metadata —
  including the **verb-name collision rule** when beherouter flattens space-joined names to underscores
  (e.g. a group `read multi` and a leaf `read_multi` would both flatten to `behemem_read_multi`).
  Resolve here (likely: forbid underscores in verb names, or reserve the space→underscore mapping and
  detect collisions at `attach` time).
- Whether `describe` is hidden from the dashboard verb menu (lean: yes — it's plumbing).
- The precise `conformance.scenarios.toml` schema (only when the first tool needs category checks).
- Moving this spec into the `beheaxi` repo as that repo's first commit.

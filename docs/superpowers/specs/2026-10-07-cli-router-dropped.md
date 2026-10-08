# Decision: the "CLI router" mode is dropped (2026-10-07)

> **Status: decided by the user, 2026-10-07.** This closes Tier 2 item 4 of
> `docs/handoffs/from-BEHEMOTION/PLANNED-BUT-UNBUILT-CAPABILITIES.md`. Reopening
> it needs a new brainstorm, spec and plan, not a reading of the rename spec.

## What was promised

The 2026-08-02 rename (`2026-08-02-beherouter-rename-design.md`) justified
"router" over "mcp" with a second mode: "an MCP gateway *and* a CLI router".
That mode was never designed. Two days later the agent-gateway spec
(`2026-08-04-…`) listed "No CLI consumer mode … `beherouter call …`" as a
non-goal. The promise survived only in `AGENTS.md` § "Why router" and the
umbrella README.

## Decision

**Dropped.** beherouter is an MCP gateway. It reaches command-line tools in one
direction only, as backends:

- `beheaxi-cli` attaches any beheaxi CLI as a surface. It is generic, its
  probe is mandatory, and it shipped on 2026-10-07.
- An agent calls those tools over MCP like any other surface's, with pinned
  tools plus `search_tools` / `describe_tool` / `run_tool`.

No mode exposes the gateway's surfaces to a shell caller (`beherouter call
<surface> <tool> …`). An operator who needs one-off calls already has
`beherouter health --deep` for probes, and any MCP client for everything else.

## Why

- The value a CLI router would add, one place that reaches every tool, is
  already delivered by the gateway plus `beheaxi-cli`, with the gateway's
  auth, identity and search on the path.
- A second consumer mode doubles the surface that identity, authz and error
  mapping must be right on, and no consumer asked for it.
- The name stays. Renaming back would cost every client config and secret
  name, and "router" still describes what the gateway does: it routes agents
  to backends.

## Consequences

- `AGENTS.md` § "Why router" now records this decision instead of a pending
  capability.
- The umbrella README stops saying the capability "is not yet built".

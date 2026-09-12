# Handoffs — beherouter

> This file is the source of truth for cross-repository handoffs in this project.
> The `/handoff` skill reads the **Repository map** below to resolve targets.
>
> Bootstrap or re-bootstrap this file with `/handoff init`. Distribute to siblings
> with `bash .claude/skills/handoff/scripts/sync.sh` (or the equivalent path under
> `.opencode/skills/`).

## What this repo is

**Slug:** `beherouter`
**Directory:** `beherouter`
**Self?** Yes — when `/handoff` runs from this repo, the source slug is `beherouter`.

The `beherouter` token is substituted by `scripts/sync.sh` when this template is
distributed to a sibling repo. If you edit this file directly, replace it with
your repo's directory name.

## How to write a proper handoff

A handoff is a written contract between two agents — usually working in different
repos, sometimes the same one — that captures everything the receiving side needs
to act. It is not a status update. It is not a chat message. It is a load-bearing
document that the receiving team will reference, push back on, and reply to.

Short handoffs are fine when the ask is genuinely small; long handoffs are fine
when the contract is genuinely complex. The structure is the same either way.

### When to write one

- A task has reached a transition point and another team needs to act before the
  work can continue.
- A contract change between two services is being proposed (request shapes, event
  names, env vars, schema columns).
- An incident has been investigated to the point where ownership crosses a repo
  boundary and the receiving team needs the forensic write-up.
- You're stepping away from a task and want the next agent in the same repo to
  pick up cleanly — `/handoff self`.

### What to include

The skill template enforces five required sections:

1. **Context** — the situation, the why, the background the receiver may not
   have.
2. **What's needed** — the specific ask. If a contract, the wire shape. If a fix,
   what specifically.
3. **Affected code / surfaces** — file paths with line numbers, container names,
   URLs, deploy targets. Concrete enough that the receiver can navigate without
   asking.
4. **Verification** — how the receiver proves the handoff is satisfied. Commands,
   smoke tests, expected outputs.
5. **Open questions** — things the sender isn't sure about. Invites pushback.

Optional sections that earn their place when the handoff is incident-driven or
contract-heavy:

- **Timeline** — a numbered table of issue/status rows when the handoff has
  evolved through several phases.
- **Smoke-test fixture** — a sample payload or event stream the receiver can
  replay against their implementation.
- **Appendix from <other team>** — relayed context from a third repo, when the
  handoff sits between three teams.
- **Replies-to** — link to an earlier handoff this one responds to.

### Addressing and ownership

- The **From** field is your repo's slug. The **To** field is the resolved target
  slug (or `self`).
- If the receiving team is downstream of multiple repos, name them explicitly in
  the Context section. Don't make the reader guess who owns what.
- Sign off with `— <slug> team, <YYYY-MM-DD>` on the last line. This becomes the
  audit trail when the timeline grows.

### Replies and supersedes

When you reply to an existing handoff, write a new file with `-REPLY` appended to
the topic and link the original in the Context block. When you supersede a
previous handoff (your understanding has changed), write a new file under the
same topic and explicitly note "supersedes the YYYY-MM-DD handoff" in Context.

## How handoffs are delivered

A handoff written via `/handoff <target>` lands in the **target** repo's tree at:

```
<target-repo>/docs/handoffs/from-<source-slug>/<TOPIC>.md
```

Self-handoffs (`/handoff self`) land at:

```
./docs/handoffs/self/<TOPIC>.md
```

The skill writes the file directly into the target's filesystem — cross-repo
writes don't go through git remotes. Each repo's owner reviews and commits the
inbound handoff on their own.

## Repository map

<!--
  One row per sibling repo. Slug must equal the sibling's directory name
  (case-sensitive). Delete this comment and the example rows below once you've
  populated your real siblings.

  For single-repo projects: leave the table body empty (header rows only). The
  skill will still allow `/handoff self`; cross-repo handoffs will be refused
  with a clear message.
-->

| Slug (= directory name) | Tag | Purpose |
|-------------------------|-----|---------|
| beherouter             | —   | This repo |

Slug = full repo directory name. Case-sensitive. The resolver is exact-match
only — no aliases, no fuzzy matching.

## How the skill resolves the argument

1. `self` → write to `./docs/handoffs/self/<TOPIC>.md`
2. Exact case-sensitive match on the **Slug** column → write to
   `../<slug>/docs/handoffs/from-beherouter/<TOPIC>.md`
3. No match → skill prints the table above and asks for clarification.

## Canonical skill source

The `/handoff` skill folder ships with everything needed to bootstrap, operate,
and distribute itself. To re-sync the skill folder + this `HANDOFF.md` to all
sibling repos listed in the Repository map:

```
bash .claude/skills/handoff/scripts/sync.sh
```

The script auto-detects which agent skill layouts exist in this repo
(`.claude/skills/handoff/`, `.opencode/skills/handoff/`, …) and replicates the
same set to each sibling, substituting `beherouter` per repo.

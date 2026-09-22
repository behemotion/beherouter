---
name: land-clean
description: Use when work in this repo is finished (or a branch/working tree full of finished work exists) and it must be committed and merged to main with all AI-agent watermarks removed from history. Takes the latest work as-is — uncommitted changes, stashes, and/or the current feature branch — writes proper commits, strips agent attribution from commit messages and content, merges to main, and pushes. On merge conflicts or ambiguity, prefers the latest work.
---

# /land-clean

Land the latest work on `main` with a clean, human-authored-looking history:
proper commits, no AI watermarks, no broken tree. Runs in six steps. Do not
skip the verification halves of steps 2, 3, and 5 — they are what makes the
rewrite safe.

The skill is runtime-portable: every instruction is a plain action (run X,
check Y, ask the user). "Ask the user" means whatever interactive primitive
the runtime offers.

## Step 0 — Never do these

- **Never rewrite commits already pushed to a shared branch** without asking
  the user first (force-pushing rewritten history breaks collaborators). If the
  target commits exist on `origin/main`, stop and ask. Commits on a local-only
  branch, or on `main` not yet pushed, are safe to rewrite.
- **Never strip a human `Co-Authored-By`**. Only trailer/footer lines that
  identify an AI agent are watermarks (see Step 2's inventory).
- **Never change file content while cleaning messages.** The tree hash before
  and after the rewrite must be byte-identical (Step 3 verifies this).
- **Never merge with a red test suite** (Step 5).

## Step 1 — Take stock of the latest work

Run, and read the output before doing anything:

```
git status --short --branch
git stash list
git branch -vv
git log --oneline -15
git log <base>..<branch> --oneline        # base = main (or origin/main if ahead)
```

"Latest work" means everything not yet on the pushed `main`: uncommitted
changes, untracked-but-not-ignored files, stashes that belong to this effort,
and any local commits on the current branch or unpushed `main`.

- If there are **uncommitted changes**: group them into logical commits now
  (see Step 4). Do not `git add -A` blindly — check `git status` for secrets,
  `.env` files, or local-only dirs that the repo's `.gitignore` already
  excludes.
- If a **stash** clearly belongs to this effort, `git stash pop` it into the
  working tree and let it join the grouping.
- If **several branches** carry finished work, ask the user which to land;
  default to the current one.

## Step 2 — Scan for watermarks

Two separate scans. Messages are always cleaned; content is cleaned only when
a hit is a true watermark, never a legitimate mention.

### 2a. Commit messages (always a watermark when matched)

Scan every commit being landed:

```
git log <base>..<branch> --format='=== %h%n%B'
```

Line-anchored watermark shapes (any of these = strip the line):

| Pattern | Examples |
|---|---|
| `Co-Authored-By: <agent>` | `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`, `Co-authored-by: Copilot <198982749+Copilot@users.noreply.github.com>`, `Co-Authored-By: Codex <codex@openai.com>`, `Co-authored-by: Cursor Agent <cursor@cursor.sh>` |
| `Generated with …` | `Generated with Claude Code`, `🤖 Generated with [Claude Code](…)`, `Generated with GitHub Copilot` |
| bare emoji footer lines | `🤖` alone on a line |
| agent-identity author/committer emails | `*@anthropic.com`, `*@openai.com`, `Copilot@users.noreply.github.com`, `noreply@cursor.sh`, `gemini-cli*` — check with `git log --format='%an|%ae|%cn|%ce' \| sort -u` |

Vendor terms to match inside trailer lines only (case-insensitive):
claude, anthropic, copilot, codex, cursor, gemini, windsurf, aider, opencode,
openai. A trailer line matching one of these terms is a watermark even in an
unlisted shape.

### 2b. File content (hit ≠ watermark — judge each)

```
git grep -inE 'claude|anthropic|copilot|codex|cursor|generated with (claude|ai)|🤖' <branch> -- . ':(exclude)CLAUDE.md' ':(exclude).claude' ':(exclude)docs/handoffs'
```

Keep (legitimate mentions — do NOT touch):

- Product/consumer names in code, tests, and tables (`claude-code` as an MCP
  client name, `test_claude_code_shape`).
- `.gitignore` / `.containerignore` entries for `.claude/`, `CLAUDE.md` —
  these keep agent dirs *out* of the repo; removing them does the opposite of
  the skill's job.
- Docs that discuss the agent files as subject matter ("scrub CLAUDE.md
  before release"), and cross-repo pointers like `$OTHER_REPO/CLAUDE.md § x`.

Strip (true content watermarks — ask the user before removing, then commit
the removal as its own commit):

- Header/footer comments in source: `// Generated with Claude Code`,
  `<!-- AI-generated -->`, `# Written by Copilot`.
- Changelog entries attributing work to an agent rather than a human.

When a hit is ambiguous, list it and ask. Never bulk-delete.

## Step 3 — Clean the history

Prerequisites: `git-filter-repo` on PATH (`which git-filter-repo`). If absent,
fall back to `git filter-branch -f --msg-filter` over the same range.

Record the tree hash first, then rewrite the branch's messages only:

```
git rev-parse <branch>^{tree}                      # BEFORE — write it down
git filter-repo --refs <branch> --force --message-callback '
import re
wm = re.compile(rb"^(co-authored-by:.*(\bclaude\b|\banthropic\b|\bcopilot\b|\bcodex\b|\bcursor\b|\bgemini\b|\bwindsurf\b|\baider\b|\bopencode\b|\bopenai\b)|generated with |$)", re.I)
kept = [l for l in message.split(b"\n") if not wm.match(l) and l != b"\xf0\x9f\xa4\x96"]
message = b"\n".join(kept).rstrip(b"\n")
return message + (b"\n" if message else b"")
'
```

(The callback is a guide, not gospel — adapt it to the shapes actually found
in Step 2a. If author/committer emails are watermarked, add
`--email-callback` / `--name-callback`; stop and confirm with the user first,
since that changes attributed authorship.)

Then verify ALL of:

```
git rev-parse <branch>^{tree}                      # must equal BEFORE
git log <base>..<branch> --format='%B' | grep -icE 'claude|anthropic|copilot|codex|cursor|generated with'   # must print 0
git log <base>..<branch> --oneline                 # same subjects, same count
```

If the tree hash differs, something rewrote content — restore
(`git reset --hard <old-tip>`) and redo with message-callback only.

If Step 2b approved content edits, make them now as ordinary commits on the
branch.

## Step 4 — Write proper commits (only for uncommitted work)

Already-committed history keeps its commits; only the working tree needs new
ones. Rules:

- One logical change per commit; subject ≤ 72 chars, imperative mood, no
  ticket noise; body explains *why*, not *what* (the diff says what).
- Separate commits for: code, docs, generated/config plumbing — when they are
  independently revertable. Fold trivially-coupled changes together instead.
- Never include secrets, `.env`, tokens. Never commit files the repo's
  `.gitignore` deliberately excludes.
- No agent attribution in the new messages (they would just be Step 3's input
  again).

## Step 5 — Verify, then merge

1. Run the repo's test suite (discover it: `uv run pytest -q` / `npm test` /
   `just test` / CI config). A merge with a red suite is not a landing.
2. Merge in the repo's own history style:
   - **Linear history** (`git log main --merges` empty) → `git checkout main
     && git merge --ff-only <branch>`.
   - **Merge-commit style** → `git merge --no-ff <branch>` with a message
     naming the feature.
3. **Conflicts: take the latest work.** "Latest" = the incoming branch's
   state, which this skill exists to land. For each conflict: prefer the
   branch's hunk unless it resurrects something the base deliberately deleted
   (then re-delete manually and note it). After resolving, re-run the tests
   before committing the merge — semantic conflicts survive textual
   resolution.
4. Delete the landed branch (`git branch -d <branch>` — safe delete only).
5. `git push origin main`. Never force-push. If the push is rejected because
   `origin/main` moved: `git fetch`, re-merge (Step 5.2 again from the new
   base), re-verify, push.

## Step 6 — Report

Tell the user, concretely:

- Commits written (subjects) and commits rewritten (count + what was stripped).
- Tree-hash check result and test-suite result.
- The merge (ff or merge commit) and the pushed ref.
- Any content hits left in place as legitimate mentions, with one-line reasons.
- Anything declined to do and why (e.g. pushed-shared-branch rewrite refused).

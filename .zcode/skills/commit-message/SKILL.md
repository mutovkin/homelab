---
name: commit-message
description: >-
  Format for every git commit message in this repo: conventional subject plus
  a body with # Why and # How sections. Use whenever creating or amending a
  commit here, including commits made inside change-loop/orchestrator runs.
  Sibling skill: pr-message (PR bodies get the same sections plus
  # Verification).
---

# Commit Message Format

Every commit in this repository carries a structured body so a future agent
reading `git log` can reconstruct the development progress without
re-deriving it from diffs. The history is a design journal, not a changelog.

## Format

```
<type>(<scope>)?: <imperative summary> (#<issue>)

# Why

<The problem or motivation, in 1–4 sentences, written for a reader who knows
nothing about the session that produced this commit. What made the change
necessary — the bug, the regression, the operational pain, the
future-proofing. Never a restatement of the diff.>

# How

<The approach at design level, organized as BULLETS — one point per bullet.
Each bullet is one discrete idea: the strategy chosen, a key decision, a
tradeoff, a non-obvious mechanism. Large blocks of prose are hard to discern
and give history no structure — don't write them. NOT a file-by-file
changelog — the diff already says that. Ask: "what would an agent reviewing
history a year from now need to understand how this project evolved?">
```

## Rules

- **Subject:** imperative, conventional-commit style (`fix:`, `docs(zcode):`,
  `feat(guests):` …), ≤72 chars where possible. Append `(#<issue>)` when
  landing a tracked issue; the squash-merge adds `(#<PR>)` automatically.
- **`# Why` answers "why does this change exist at all?"** — not what
  changed.
- **`# How` answers "what was the approach?"** — mechanism and decisions as
  bullets, one idea each (e.g. "mask AppArmor before Docker config,
  per-service `security_opt`"), not file inventory.
- Mechanical changes (rename, typo, tag bump) get a single bullet per
  section — but the sections are **never omitted**.
- **No AI attribution trailers** of any kind.
- Write the body with a heredoc so the sections and blank lines survive:
  `git commit -m "$(cat <<'EOF' … EOF)"`.

## Example (real history)

```
docs(zcode): migrate orchestrator + homelab-change-loop skills to ZCode

# Why

The repo's automation skills lived only under .claude/skills/ (Claude Code);
driving the homelab from ZCode needs them discoverable at .zcode/skills/.
The .claude originals stay untouched — both clients keep working.

# How

- Coordinator/implementer are plain background general-purpose Agents (ZCode has no model picker, worktree isolation, or fork).
- Fix rounds resume the same agent via SendMessage; worktrees are created by the coordinator itself.
- Plugin references stay namespaced (superpowers:writing-plans, compound-engineering:ce-compound).
- pr-review-toolkit personas travel in reviewer spawn prompts — ZCode has no plugin-agent registry.
- Safety protocol unchanged (vault bootstrap, spinlock, backups, waves).
```

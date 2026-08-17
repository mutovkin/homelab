---
name: pr-message
description: >-
  Format for pull-request titles and bodies in this repo: # Why, # How, and a
  required # Verification section, plus a Mermaid diagram only when it aids
  understanding of the How. Use whenever creating a PR (gh pr create) or
  editing an open PR's body. Sibling skill: commit-message (same Why/How
  discipline for commit bodies).
---

# PR Message Format

A PR body is the durable record of a change: the squash-merge carries it onto
master, so a future agent reading history gets the full story — what
motivated the change, how it was approached, and **proof it was verified on
the live hosts, not assumed**.

## Title

```
<type>(<scope>)?: <imperative summary> (#<issue>)
```

Same conventional-commit discipline as commit subjects (see the
`commit-message` skill). The PR number is appended by the squash-merge.

## Body sections, in order

### `# Why`

The problem or motivation, for a zero-context reader — same discipline as the
commit `# Why`: the bug, the regression, the operational pain. Never a
restatement of the diff.

### `# How`

The approach at design level — organized as bullets, one discrete point per
bullet (strategy, key decisions, tradeoffs, non-obvious mechanics). Never a
wall of prose, and not a file-by-file changelog: large blocks of text are
harder to discern and give reviewers no structure.

**Mermaid diagram — append only if one would genuinely assist understanding of
the How.** Good candidates: before/after network or service topology, request
flow through components, sequence of a state transition. Use a fenced
```mermaid block (GitHub renders it natively), keep it small (under ~20
lines), and label before vs after. Skip it for docs-only, single-file, or
otherwise self-evident changes — a decorative diagram costs more attention
than it buys.

### `# Verification` — REQUIRED on every PR

How the PR was proven correct, with **pasted evidence, not claims**. For
infrastructure changes the evidence comes straight out of
`homelab-change-loop` steps 3–6:

- **Static:** `ansible-lint` + `--syntax-check` results (both clean).
- **Dry-run:** `--check --diff --limit <host>` PLAY RECAP (`failed=0`) and a
  summary of the reported drift.
- **Live apply:** PLAY RECAP of the real run.
- **End-state assertions:** the actual read-only command and its output
  proving the fix on the host (e.g. `SHOW hba_file;`, `systemctl is-active`,
  `nft list table` probe from a blocked source).
- **Idempotency:** second-run recap showing the relevant tasks
  `ok`/`skipping`, not `changed`.

If a category doesn't apply (docs-only PR), say so explicitly under
`# Verification` — never drop the section silently.

## Rules

- **No AI attribution trailers** of any kind.
- Create with:
  `gh pr create --title "<title>" --body "$(cat <<'EOF' … EOF)" --base master --head <branch>`
- Mermaid fences and pasted recaps both survive heredocs fine; keep PLAY
  RECAP excerpts trimmed to the hosts actually in scope.

## Skeleton

```
# Why

<1–4 sentences>

# How

- <design-level point>
- <design-level point>
[optional: ```mermaid diagram```]

# Verification

- Static: ansible-lint clean, syntax-check clean
- Dry-run (--check --diff --limit <host>): PLAY RECAP failed=0; <drift summary>
- Apply: PLAY RECAP failed=0
- End-state: `<command>` → <output>
- Idempotency: second run <tasks> ok/skipping
```

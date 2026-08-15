---
name: orchestrator
description: >-
  Orchestrate a multi-issue fix run across parallel git worktrees. The invoking
  session does NO implementation work: per-issue coordinator forks (Fable,
  context-rich, zero edits) drive Opus workers through the homelab-change-loop
  (implement → lint → dry-run → backup → live apply → verify → review → fix →
  compound → PR → squash-merge). Use when landing a batch of tracked GitHub
  issues autonomously.
---

# Orchestrator

Wraps `homelab-change-loop` (one trip per issue) in a scheduler that runs many
trips concurrently and safely. The orchestrator session sequences waves, spawns
coordinators, holds risk gates, and closes out — it never edits repo files
itself.

## Roles

| Role | Who | Does | Never does |
| ---- | --- | ---- | ---------- |
| Orchestrator | invoking session | wave scheduling, spawning forks, merge tracking, risk-gate pauses, close-out report | file edits, deploys |
| Coordinator (per issue) | FRESH Fable agent (`model: "fable"`, `isolation: "worktree"`) — briefed with the issue, the skill, and pointers to CLAUDE.md gotchas + `docs/solutions/`. Fork (`subagent_type: "fork"`) only when decisive knowledge exists solely in the invoking session's conversation; the PLAN step + detailed issues + compounded docs normally make forking unnecessary and it drags the whole session prefix through every request | authors the per-issue PLAN, drives the loop, adversarial review + re-review, escalates gates | repo file edits — ALL implementation delegated to its Opus worker (its only writes are its own plan/notes under `.local-notes/`) |
| Implementer | Opus agent (`model: "opus"`), spawned by coordinator | branch, code, lint, dry-run, backup, apply, verify, docs, commits, PR | merging without a clean coordinator re-review |

Fix rounds continue the SAME Opus agent via SendMessage (it keeps context).
The coordinator is also the final adversarial reviewer — the same Fable session
reviews, demands fixes, and re-reviews the deployed revision.

## Per-issue pipeline (coordinator runs this)

1. Re-validate the issue against the CURRENT tree (issue text may predate
   restructures). Re-map file paths before implementing.
2. **PLAN (Fable, before any worker exists).** The coordinator writes a
   detailed spec to `.local-notes/issue-<n>-plan.md` (gitignored):
   - problem restated against the current tree, with corrected file paths
   - exact files to change and the approach per file
   - ordered implementation steps
   - verification commands WITH expected outputs (static, dry-run, live
     end-state, idempotency)
   - acceptance criteria and explicit OUT-OF-SCOPE list
   - risk gates and backup requirements for this specific change
   Format: follow `superpowers:writing-plans` (chosen over ce-plan: it is
   built for a zero-context executor and has no interactive handoff
   contract), adapted to this stack — the per-task verify cycle is
   lint → `--check --diff` → apply → end-state assert instead of TDD, and
   its execution-handoff menu is skipped (this pipeline defines execution).
   Honor its No Placeholders rule literally.
   The worker EXECUTES this plan; any deviation needs coordinator sign-off
   via SendMessage before it lands. Reviewers later check the diff for
   conformance to the plan, and the plan for having been right.
3. Opus implements on branch `fix/<issue>-<slug>` off fresh master,
   following the plan.
3. `homelab-change-loop` steps 3–6: ansible-lint + syntax-check → `--check
   --diff --limit <host>` → **backup** → apply (locked) → end-state verify +
   second-run idempotency.
4. `/pr-review-toolkit:review-pr` agents on the branch diff.
5. Coordinator (Fable) adversarial review → APPROVE / CHANGES NEEDED.
6. CHANGES → same Opus worker fixes → back to step 3 → coordinator re-reviews.
   Nothing merges that wasn't re-verified after its last code change.
7. Docs update (README/CLAUDE.md/docs affected by the change) + `ce-compound`
   (mode:headless) if the change taught a non-obvious lesson — last commits on
   the branch.
8. PR → rebase on latest master → squash-merge (`Fixes #<n>`), delete branch.

## Formats (enforced)

- **Commits:** body contains `# Why` and `# How` sections.
- **PR body:** Why/How + a **Mermaid diagram** (before/after topology, flow, or
  sequence — whichever fits) + live verification evidence (PLAY RECAP,
  end-state assertion output, idempotency second run).

## Concurrency & safety protocol

- **Worktree bootstrap** (gitignored files are absent in worktrees):
  `export ANSIBLE_VAULT_PASSWORD_FILE=<main-checkout>/.vault_password`
  Do NOT export `ANSIBLE_COLLECTIONS_PATH` to `<main-checkout>/ansible/.ansible/collections`
  — that directory holds ansible-lint's auto-generated MOCK module stubs (empty
  argument_spec) and breaks real playbook runs. Real collections resolve from
  `~/.ansible/collections` by default; leave the path untouched.
- **Per-host apply lock** — `flock` does NOT exist on macOS (the control
  machine); use a portable mkdir spinlock around every live apply:
  ```sh
  L=/tmp/homelab-deploy-<host>.lock.d
  until mkdir "$L" 2>/dev/null; do sleep 5; done
  trap 'rmdir "$L"' EXIT
  ansible-playbook … --limit <host>
  ```
  Never pipe the apply through `tail`/`grep` — pipes mask the exit code; write
  to a log file and check `$?` explicitly.
- **Backups before every live apply:** ZFS snapshot of the target CT's /data
  subvol on its Proxmox host (`zfs snapshot <dataset>@pre-issue<NN>-<ts>`;
  discover dataset via `pct config <vmid>`); plus `pg_dumpall` to
  `/data/backups/` for any postgres-adjacent change. Snapshots persist until
  the run's close-out and are pruned only after the human confirms.
- **Wave planning:** pair issues only when their file sets are disjoint;
  fleet-wide sweeps (all-compose edits, tree restructures) run solo; dependent
  issues run in later waves than their prerequisites. Docs-only issues combine
  into one branch, last, no deploy.
- **Merge queue:** rebase on master before merge; on push race, re-rebase and
  retry.

## Risk gates (coordinator pauses; orchestrator asks the human)

- Live network reconfig or anything that bounces every stack on a host.
- Changes with a user-visible outage window on a stateful service.
- Anything irreversible (disk/ZFS destroy, data migration).
- Manual out-of-repo steps (e.g. NPM proxy UI changes).

## Topology facts coordinators must honor

- NPM lives in its own LXC — binding service ports to 127.0.0.1 on a docker
  host breaks NPM's upstream reach. Restrict exposure with scoped nftables
  allowlists instead (`docs/solutions/conventions/scoped-nftables-on-live-host.md`).
- All changes via Ansible; SSH is read-only diagnostics (+ the backup commands
  above, which are explicitly sanctioned).

## Close-out (orchestrator)

- Fleet-wide `task deploy:full -- --check --diff` ⇒ zero pending changes.
- All issues CLOSED (or commented why partial), branches deleted, CI green.
- One batched `ce-compound` for run-wide lessons.
- Report: per-issue outcome table, deploys performed, snapshots taken/pruned,
  escalations.

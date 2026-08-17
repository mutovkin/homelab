---
name: orchestrator
description: >-
  Orchestrate a multi-issue fix run across parallel git worktrees. The invoking
  session does NO implementation work: per-issue coordinator agents
  (context-rich, zero edits) drive implementer agents through the
  homelab-change-loop (implement → lint → dry-run → backup → live apply →
  verify → review → fix → compound → PR → squash-merge). Use when landing a
  batch of tracked GitHub issues autonomously.
---

# Orchestrator

Wraps `homelab-change-loop` (one trip per issue) in a scheduler that runs many
trips concurrently and safely. The orchestrator session sequences waves, spawns
coordinators, holds risk gates, and closes out — it never edits repo files
itself.

## ZCode mechanics (read first)

ZCode's Agent tool has no model picker, no worktree isolation, and no fork
mechanism — everything the Claude Code version got from those options is done
explicitly here:

- **Spawning:** Agent tool, `subagent_type: "general-purpose"`,
  `run_in_background: true`. A new agent starts with ZERO conversation context:
  its prompt must be fully self-contained (issue text, this skill's name,
  worktree path, vault-password bootstrap, gotcha pointers). If decisive
  knowledge exists solely in the invoking session's conversation, it goes IN the
  spawn prompt — there is no fork to inherit it from.
- **Resuming:** `SendMessage` to the agent's `agent_<id>` (returned by the
  Agent call) resumes that same agent with its context intact. This is how fix
  rounds keep the same implementer and how the coordinator does its final
  adversarial re-review.
- **Worktrees:** each coordinator creates its own —
  `git worktree add <path> -b fix/<issue>-<slug> master` (run inside the
  coordinator, not the orchestrator).
- **Tracking:** background agents notify the orchestrating session on
  completion; `TaskOutput` polls a running/completed task by id when needed.
- **Plugin skills** are invoked namespaced: `superpowers:writing-plans`,
  `compound-engineering:ce-compound`.

## Roles

| Role | Who | Does | Never does |
| ---- | --- | ---- | ---------- |
| Orchestrator | invoking session | wave scheduling, spawning background agents, merge tracking, risk-gate pauses, close-out report | file edits, deploys |
| Coordinator (per issue) | FRESH background `general-purpose` Agent — briefed in the spawn prompt with the issue, the `homelab-change-loop` skill name, and pointers to CLAUDE.md gotchas + `docs/solutions/`. Creates its own worktree, spawns its implementer, resumes both via SendMessage | authors the per-issue PLAN, drives the loop, adversarial review + re-review, escalates gates | repo file edits — ALL implementation delegated to its implementer agent (its only writes are its own plan/notes under `.local-notes/`) |
| Implementer | second `general-purpose` Agent, spawned by the coordinator (works in the coordinator's worktree) | branch, code, lint, dry-run, backup, apply, verify, docs, commits, PR | merging without a clean coordinator re-review |

Fix rounds continue the SAME implementer via SendMessage (it keeps context).
The coordinator is also the final adversarial reviewer — resuming that same
agent reviews, demands fixes, and re-reviews the deployed revision.

## Per-issue pipeline (coordinator runs this)

1. Re-validate the issue against the CURRENT tree (issue text may predate
   restructures). Re-map file paths before implementing.
2. **PLAN (before any implementer exists).** The coordinator writes a
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
   The implementer EXECUTES this plan; any deviation needs coordinator sign-off
   via SendMessage before it lands. Reviewers later check the diff for
   conformance to the plan, and the plan for having been right.
3. Implementer implements on branch `fix/<issue>-<slug>` off fresh master
   (already created with the worktree), following the plan.
4. `homelab-change-loop` steps 3–6: ansible-lint + syntax-check → `--check
   --diff --limit <host>` → **backup** → apply (locked) → end-state verify +
   second-run idempotency.
5. Review agents on the branch diff: spawn parallel `general-purpose` agents,
   one per review aspect, each briefed with a persona from the pr-review-toolkit
   plugin's `agents/` directory
   (`~/.zcode/cli/plugins/cache/claude-plugins-official/pr-review-toolkit/0.0.0/agents/`
   — `code-reviewer.md`, `silent-failure-hunter.md`, etc. — read the file and
   fold its instructions into the spawn prompt; ZCode has no plugin-agent
   registry, so the personas must travel in the prompt).
6. Coordinator adversarial review → APPROVE / CHANGES NEEDED.
7. CHANGES → same implementer fixes (SendMessage) → back to step 4 →
   coordinator re-reviews. Nothing merges that wasn't re-verified after its
   last code change.
8. Docs update (README/CLAUDE.md/docs affected by the change) +
   `compound-engineering:ce-compound` (mode:headless) if the change taught a
   non-obvious lesson — last commits on the branch.
9. PR → rebase on latest master → squash-merge (`Fixes #<n>`), delete branch.

## Formats (enforced)

- **Commits:** body contains `# Why` and `# How` sections.
- **PR body:** Why/How + a **Mermaid diagram** (before/after topology, flow, or
  sequence — whichever fits) + live verification evidence (PLAY RECAP,
  end-state assertion output, idempotency second run).
- **No AI attribution trailer** in any commit or PR text.

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
  `/data/backups/` for any postgres-adjacent change — as
  `docker exec -u postgres postgres pg_dumpall` (socket auth is `peer` since
  #79: a root invocation fails and a redirected one silently yields an empty
  file; always verify the dump ends with "cluster dump complete"). Snapshots persist until
  the run's close-out and are pruned only after the human confirms.
- **Wave planning:** pair issues only when their file sets are disjoint;
  fleet-wide sweeps (all-compose edits, tree restructures) run solo; dependent
  issues run in later waves than their prerequisites. Docs-only issues combine
  into one branch, last, no deploy.
- **Wave execution:** spawn each wave's coordinator Agents in a single message
  (parallel background); wait for all completions (notifications / TaskOutput)
  before evaluating gates or scheduling the next wave.
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
- One batched `compound-engineering:ce-compound` for run-wide lessons.
- Report: per-issue outcome table, deploys performed, snapshots taken/pruned,
  escalations.

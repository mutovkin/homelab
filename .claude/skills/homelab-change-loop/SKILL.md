---
name: homelab-change-loop
description: >-
  Bulletproof loop for making a change to this homelab repo end-to-end — issue →
  branch → implement → verify locally → deploy live → multi-agent review → fix &
  re-verify until clean → merge. Use whenever implementing a tracked fix/feature
  in the Ansible/Docker-Compose homelab, especially when changes will be applied
  to the live eq12/n5pro hosts. Invoke per issue; each issue is one trip through
  the loop.
---

# Homelab Change Loop

A disciplined, repeatable loop for landing a change safely in this repo. The
governing rule of the homelab applies throughout: **all changes go through
Ansible, never ad-hoc SSH** (SSH is read-only diagnostics). Every change must be
**proven against the live hosts before it is merged**, not after — and every
non-obvious lesson must be **compounded into `docs/solutions/`** so the loop gets
smarter each iteration instead of repeating mistakes.

## When to use

One trip through the loop per tracked unit of work (a GitHub issue). Use it for
any Ansible role / inventory / compose change. For pure-docs changes the deploy
steps collapse to "n/a" but the review + merge steps still apply.

## The loop (do the steps in order)

```
0. SCOPE      — one issue, understood; capture discovered work as new issues
1. BRANCH     — feature branch off fresh master
2. IMPLEMENT  — smallest change that fixes the issue
3. STATIC     — ansible-lint + syntax-check (regression bar: both clean)
4. DRY-RUN    — --check --diff against the LIVE hosts (scoped with --limit)
5. APPLY      — run for real against the live hosts (BEFORE merge)
6. VERIFY     — assert the actual end-state on the host (service active, value set…)
7. REVIEW     — aggressive multi-agent review of the branch diff
8. TRIAGE     — real findings? → fix → GO TO 3 (re-test, re-deploy, re-review)
                clean? → continue
9. COMPOUND   — if the change taught something non-obvious, capture it via
                ce-compound as the LAST commit ON THE BRANCH (before merge)
10. MERGE     — squash-merge into master, closes the issue, delete branch
11. SYNC      — checkout master, pull, confirm issue CLOSED
```

The hinge is **step 8**. Review is not a rubber stamp before merge — a real
finding sends you back to step 3 and you go around again (fix → static →
dry-run → apply → verify → review) until a review pass is clean. Only a clean
review reaches merge. This is what makes the loop bulletproof: **nothing merges
that hasn't been re-verified after its last code change.**

## Step detail

### 0. Scope
- Confirm the issue is understood and is a single unit of work. If it sprawls,
  split it.
- Anything you discover along the way that is out of scope → **file a new issue**
  (`gh issue create`), don't silently fix it or drop it. (This session filed
  #20 and #23 this way.)

### 1. Branch
```bash
git checkout master && git pull
git checkout -b fix/<n>-<slug> master
```

### 2. Implement
- Smallest change that closes the issue. Follow existing role patterns.
- Reuse the repo's conventions (e.g. `become: false` on `synchronize`, version
  pinned in one var, `security_opt: apparmor:unconfined` on new compose services).

### 3. Static checks (regression bar)
```bash
cd ansible
ansible-lint roles/<role>/ -q          # must be clean
ansible-playbook playbooks/site.yml --syntax-check
```
Baseline is clean — keep it clean. (YAML is linted via `ansible-lint`'s bundled
yamllint rules — no separate `yamllint` invocation.)

### 4. Dry-run against live hosts
```bash
ansible-playbook playbooks/<playbook>.yml --check --diff --limit <host>
```
- This is **real** (it connects to the hosts and reports true drift), not
  theoretical. Read the PLAY RECAP: target `failed=0`.
- `--check` skips `command`/`shell` tasks, so command-derived facts can be empty
  — code must be check-mode safe (`default('{}', true)`, `check_mode: false` on
  read-only lookups). A `--check` failure that only happens in check mode is
  still a real defect: the documented dry-run-before-apply workflow must work.

### 5. Apply to live hosts (before merge)
```bash
ansible-playbook playbooks/<playbook>.yml --limit <host>
```
- Scope with `--limit` to the host(s) the change touches.
- Watch reachability for anything touching networking (`ping` the host during a
  bridge/network reload). `failed=0` in the recap is necessary but not
  sufficient — verify the real outcome next.
- Long runs: launch in the background and poll the output file.

### 6. Verify the real end-state
Don't trust `changed/ok` alone — assert the actual effect, read-only:
```bash
ansible <host> -m shell -a "systemctl is-active <svc>" -b      # service up?
ansible <host> -m shell -a "<cmd that proves the fix>" -b
```
For idempotency-class fixes, run the apply **twice** and confirm the second run
reports the relevant tasks as `ok`/`skipping`, not `changed`.

### 7. Aggressive multi-agent review
Review the branch diff with multiple specialized agents in parallel, not one
gentle pass. Pick reviewers by what the change touches:
- always: a correctness reviewer
- secrets/tokens/exposure: a security reviewer
- error handling / fallbacks / "trusted forever" logic: a silent-failure hunter
- structure/duplication: a maintainability reviewer
Give each agent the issue context, the branch name, and the exact diff command
(`git diff master...<branch>`). Demand a clear **APPROVE / CHANGES NEEDED**.

### 8. Triage findings — the cycle
- **Verify each finding before acting** — reviewers can be wrong. (This session
  one agent claimed `get_url` never re-validates an existing file; the live error
  log disproved it — rejected. Another correctly found the `stat` gate defeated a
  checksum — accepted.) Check the claim against real evidence.
- **Real finding → fix it → GO TO STEP 3.** Re-run static, dry-run, apply,
  verify, and review again. Do not merge a change whose latest revision hasn't
  been re-verified and re-reviewed.
- **No real findings → continue to merge.**

### 9. Compound the learning — BEFORE merge, as the last commit on the branch
If this change taught something non-obvious, **capture it via the
compound-engineering `ce-compound` skill** so the knowledge lands in
`docs/solutions/` (searchable YAML frontmatter) and the next trip through the
loop doesn't relearn it. This is what makes the loop *improve over time*.

**Do this before merge so the lesson rides in on the same PR.** The PR then tells
a complete story — "fixed X, and while doing it learned Y, here's the doc" — and
the squash-merge carries both the fix and its lesson as one coherent unit.
Compounding after merge fragments that narrative and is easy to skip.

```
# autonomous loop pass (no human to answer prompts) — use headless or it blocks:
Skill("ce-compound", "mode:headless <one-line context>")
```
or `/ce-compound <context>` when a human is driving interactively (it will ask
Full-vs-Lightweight and consent questions). Then commit the new
`docs/solutions/...` doc onto the branch as its final commit.

**Compound when** the change involved any of:
- a bug the review or a dry-run caught that wasn't obvious up front
  (e.g. the `stat` gate defeating a checksum; a `--check`-only failure),
- a discovered issue you filed mid-loop,
- a non-obvious fix or a gotcha specific to this repo's stack
  (Proxmox/Ansible/LXC/Compose behavior that surprised you),
- anything you'd want a future session to find by searching before re-debugging.

**Skip when** the change was trivial/mechanical (a rename, a one-line tag
removal, a doc typo) — no lesson to compound.

Batch is fine: after a run of related issues, one `ce-compound` pass can document
the shared theme rather than one doc per trivial issue. The goal is durable,
discoverable lessons, not volume. Cross-link the new doc from `CLAUDE.md` Gotchas
when it's a recurring trap.

### 10. Merge
```bash
gh pr create --title "...(#<n>)" --body "...verification evidence..." --base master --head <branch>
gh pr merge <pr> --squash --delete-branch
```
- Squash so each issue maps to one commit on master ending `(#<n>)` — fix +
  compounded lesson land together.
- The PR body carries the **live verification evidence** (recap, end-state
  checks), so the merge record shows it was proven, not assumed.
- Commit/PR text adds no AI attribution trailer (suppressed via
  `attribution` in `.claude/settings.json`).

### 11. Sync
```bash
git checkout master && git pull
gh issue view <n> --json state -q .state   # expect CLOSED
```

## Risk gates (stop and get explicit OK)

Apply-before-merge is the default, but pause for the human on:
- **Live network reconfig** (e.g. `pvesh set .../network` reloads the whole node)
  — have console/IPMI fallback; `--limit` the host.
- **Service recreation** (compose redeploys bounce live containers — seconds of
  downtime).
- **Anything irreversible** (disk/ZFS/destroy operations).

## Anti-patterns (what this loop forbids)

- Merging then deploying ("it passed review") — deploy and verify **first**.
- Treating a clean review as the finish line — the finish line is a clean review
  **of the last revision that was actually deployed**.
- Fixing review findings and merging without re-running the loop.
- Silently fixing or dropping discovered out-of-scope problems — file an issue.
- Ad-hoc SSH changes to "just fix it on the box" — encode in Ansible, re-run.
- Trusting `--check`-only success — and trusting `failed=0` without an end-state
  assertion.
- Landing a non-obvious fix without compounding the lesson — a trap the loop
  caught once will be rediscovered next time if it isn't written to
  `docs/solutions/` via `ce-compound`.

---
title: "A verification is only evidence if its instrument can tell the fixed state from the broken one"
date: 2026-08-19
category: conventions
module: ansible
problem_type: convention
component: tooling
severity: high
applies_when:
  - "claiming that a refactor makes something visible in `--check` that was invisible before"
  - "running an A/B of the same playbook on master and on a branch and diffing the logs"
  - "proving idempotency of an `ansible.posix.synchronize` task from a second checkout"
  - "a task reports `changed` for a payload you believe is byte-identical"
  - "a fleet is fully converged and you need to demonstrate drift-reporting behaviour"
  - "inheriting a folklore measurement (\"it needs two passes to settle\") into a new verification"
related_components:
  - proxmox_guests
  - services/_deploy
  - rsync
  - lxc
tags:
  - ansible
  - check-mode
  - idempotency
  - verification
  - rsync
  - fixture
  - silent-failure
  - review-loop
---

# A verification is only evidence if its instrument can tell the fixed state from the broken one

## Context

One batch (#135, #137, #138) produced two verifications that looked convincing and were
not. Neither failure was in the code under test — both were in the instrument used to
measure it. They failed in opposite directions, which is why they are worth one doc:

- **#135** — the proof came back *identical on both legs*, so it could not distinguish
  the fix from the bug. It looked like a pass.
- **#138** — the proof came back *failing on the fixed leg*, for a reason that had
  nothing to do with the change. It looked like a regression.

The generalisation is the same in both: before trusting a verification, ask what its
output would look like if the change had **not** worked, and confirm that is a different
output from the one you are holding. This is the same discipline as a Canary dry-run
(CONCEPTS.md) — absence means something only after presence has been demonstrated —
applied to change-reporting rather than to secret suppression.

## Guidance

### A `--check` A/B is vacuous when both legs skip for different reasons

#135 converted `Set features for privileged LXC containers` in
`ansible/roles/proxmox_guests/tasks/main.yml` from a mutate-then-echo
`ansible.builtin.shell` into the role's read → compute → report → apply chain
(`set_fact` + `debug` + `command`) over the shared `lxc_config_raw` read. The claim to
prove was: *features drift is now visible under `--check`, and it was not before.*

The obvious proof is to run `ansible-playbook playbooks/proxmox-hosts.yml --check --diff
--limit n5pro` on master and on the branch and diff the logs. Doing that produced:

```
master leg:  TASK [proxmox_guests : Set features for privileged LXC containers]
             skipping: [n5pro] => (item=CT 201 (n5pro-docker) — features)

branch leg:  TASK [proxmox_guests : Compute LXC features drift]
             skipping: [n5pro] => (item=CT 201 (n5pro-docker))
```

Both legs say `skipping`. They skip for **opposite** reasons:

- master skips because Ansible skips `shell` wholesale in check mode — it would have
  skipped **even with drift present**, and that is precisely the defect;
- the branch skips because the `when:` compare found no drift — CT 201 is converged,
  which is the desired steady state.

Pasting that pair as evidence would have "proved" the fix using output indistinguishable
from the bug. Worse, **no live host in this fleet can produce the distinguishing output at
all**: every container is converged, so the drift branch is never taken. A converged fleet
is the wrong instrument for a drift-reporting claim, and no amount of re-running fixes
that.

**The fix is a drifted fixture.** A throwaway playbook that feeds synthetic *drifted*
input to both task shapes and is run with `--check`:

```yaml
- name: Features drift check-mode visibility
  hosts: localhost
  connection: local
  gather_facts: false
  become: false          # ansible.cfg sets become = True globally — see below
  vars:
    lxc_config_raw:
      results:
        - rc: 0
          stdout: "arch: amd64\nfeatures: nesting=1\nhostname: fixture-ct\n"
          item: {vmid: 999, hostname: fixture-ct, unprivileged: false,
                 nfs_enabled: true, nesting: true}
  tasks:
    # ... the master-shaped shell task and the branch-shaped compute/report/apply
    # chain, both verbatim, both fed the same drifted input
```

Run under `--check` it separates cleanly:

```
TASK [MASTER SHAPE - Set features for privileged LXC containers]
skipping: [localhost] => (item=CT 999 (fixture-ct) — features)      <- drift INVISIBLE

TASK [Compute LXC features drift]   ok: [localhost] => (item=CT 999 (fixture-ct))
TASK [Report LXC features drift]    ok: [localhost] => {
    "msg": "CT 999 (fixture-ct): features live=nesting=1 desired=mount=nfs,nesting=1
            — will apply via pct set" }
TASK [Apply features to existing LXC containers]   skipping: [localhost]
```

Two practical notes on building one of these:

- **`ansible.cfg` in this repo sets `become = True` globally.** A `hosts: localhost`
  fixture without `become: false` dies with `sudo: a password is required` before it
  proves anything — a failure that looks like a broken fixture rather than a config
  default.
- **Swap only the side effect, never the logic.** The fixture must run the *verbatim*
  Jinja expressions and `when:` gates from the role; replace `pct set` with `echo` and
  nothing else. A fixture that paraphrases the condition tests the paraphrase.

The same fixture doubles as a truth table for the computation itself. Eight rows —
converged, reverse-ordered value, unset value, empty value, feature removal, and the
privileged-only gate — are what make an order-insensitive comparison trustworthy, and
they cost seconds compared with discovering an edge case on a hypervisor.

### `--checksum` makes rsync content-idempotent, not idempotent

[Collocating compose stacks into Ansible roles](collocating-compose-stacks-into-ansible-roles.md)
already establishes that `--checksum` decides *what to transfer, not whether mtimes
change*, teaches the itemize columns, and documents the `times: false` + `--checksum`
pair on the observability role's Grafana syncs. This is the delta, not a restatement.

#138 gave the same treatment to the shared `Synchronize deploy payload for {{ svc }}`
task in `ansible/roles/services/_deploy/tasks/main.yml` — the pipeline every service
deploys through. Verified by cloning the worktree to a second checkout at an identical
HEAD and running the same `--check` against the same host, changing only
`ansible/roles/services/_deploy/tasks/main.yml`:

```
MASTER _deploy:   .d..t...... ./
                  <f..t...... compose.yaml
                  changed: [n5pro_docker]          (recap changed=3)

BRANCH _deploy:   ok: [n5pro_docker]               (recap changed=2)
```

But the **first** attempt at that A/B returned this on the branch leg:

```
BRANCH _deploy:   .d.....g... ./
                  .f.....g... compose.yaml
                  changed: [n5pro_docker]
```

Read the letters: `<f..t..g` → `.f.....g`. `--checksum` killed the transfer (`<` → `.`)
and `times: false` killed the mtime flag (`t` gone), but **`g` — group ownership —
survived and still flipped `changed`**. The cause was environmental: the verification
clone lived under `/private/tmp`, whose group on macOS is `wheel`, while the host's copy
is owned `staff`. `chgrp -R staff` on the clone produced the clean `ok`.

So: **`--checksum` covers file CONTENT only.** Mode and ownership are compared
separately, and nothing in the `times: false` + `--checksum` pair addresses them. When a
sync reports `changed` for a payload you believe is identical, read the 11-character
itemization (`YXcstpoguax`) before blaming the content — the letter names the attribute,
and `g`/`o`/`p` mean the difference is metadata, quite possibly an artifact of where your
verification checkout happens to live.

### Do not inherit a measurement without re-deriving what it measured

The #138 work carried forward a warning that *"prior measurement showed the rsync churn
needed TWO passes to settle."* Measured honestly against the fix, it needs **one**: four
real deploys of one service (two from the worktree, two from a fresh group-normalised
clone, three different source mtimes against a host copy dated two days earlier) each
reported the synchronize task `ok` with `changed=0` on the **first** pass, and the host
file's mtime was unchanged afterwards.

Re-deriving the original claim showed it was a conflation of two unrelated observations,
neither of which was a fresh checkout against byte-identical content:

- the documented one-cycle cost of **creating a file inside a directory the same run
  syncs** (the first-ever `.env` bumping the deploy directory's mtime), which is
  reconciliation and is already recorded in the collocating doc; and
- a fleet-wide omnibus apply that reached idempotency on pass 3 while recreating five
  containers and renumbering a docker address pool — a run with many candidate causes,
  none of them this task.

`times: false` removes exactly the mtime rewrite that a second pass used to absorb, so
the folklore was also *obsoleted* by the fix it was warning about. A remembered pass
count is a claim about a past system; re-measure it against the present one before
letting it set an acceptance bar.

## Why This Matters

This repo treats `changed` as evidence — the watchtower and idempotency gotchas in
CLAUDE.md both turn on it, and a reconcile task that reports change on every run is read
as a defect rather than as noise. That convention only holds if the signal is trustworthy
in both directions. A vacuous A/B silently converts "I proved it" into "I produced
output", and a confounded idempotency test burns review cycles chasing a defect that
lives in the tester's scratch directory.

Both failure modes are cheap to catch and expensive to miss, because both produce
*plausible* output. Nothing errors. The run is green. The only thing that distinguishes a
real proof from a vacuous one is having asked, in advance, what the negative result would
have looked like.

## When to Apply

- Any claim of the form "the dry-run now surfaces X". Build an input where X is actually
  present; a converged fleet cannot demonstrate it.
- Any A/B where both legs produce the *same* verdict token (`skipping`, `ok`, `changed`).
  Same token is not the same reason — establish the reason before reading it as a result.
- Any idempotency proof run from a second checkout, clone, worktree, or machine.
  Normalise the checkout's group and permissions first, or you measure the scratch
  directory instead of the change.
- Any time an inherited number ("needs two passes", "always reports changed") is about to
  become an acceptance criterion.

## Examples

**Reading a `changed` you did not expect — diagnose from the letters, not the content:**

```
.f.....g... compose.yaml     # content identical; GROUP differs -> environment artifact
<f..t...... compose.yaml     # will transfer; mtime differs   -> the --checksum case
<f+++++++++ compose.yaml     # new file on the receiver
*deleting   old-name.yml     # delete: true retiring a repo file
```

**The shape of a non-vacuous check-mode A/B:** the two legs must differ in the *verdict*,
not merely in the task name. If master and branch both skip, both `ok`, or both report the
same count, the instrument has not been shown to discriminate — construct drifted input
until one leg reports and the other does not.

## Related

- [Collocating compose stacks into Ansible roles](collocating-compose-stacks-into-ansible-roles.md)
  — owns the rsync itemization table, the `--checksum`/`times: false` fix on the
  observability Grafana syncs, the "creating a file in an rsync'd directory costs one
  idempotency cycle" finding, and "live-vs-repo drift is invisible to a collocation
  dry-run". The `g` column and the content-vs-metadata split above extend it.
- [Ansible change-loop pitfalls](ansible-change-loop-pitfalls.md) — check-mode safety
  (`--check` skips `command`/`shell` wholesale) and "a skipped branch is an untested
  branch", the two rules this doc's first half generalises.
- [`nfs=1` is not an LXC feature key](../integration-issues/lxc-features-nfs-invalid-key-silent-green.md)
  (#86) — the defect the #135 refactor descends from; explains why the desired value is
  spelled `mount=nfs` and why the comparison is order-insensitive.
- [community.proxmox updates by default](../integration-issues/community-proxmox-update-default-blind-config-put.md)
  (#86) — establishes the read → compute → report → apply pattern and the
  `check_mode: false` probe plus `debug` drift report that make a reconcile visible in
  check mode. This doc is how you verify that such a refactor actually worked.
- #119 — extended the pattern to the NIC and startup/onboot reconciles. Its own
  Verification section prescribed injecting drift into host_vars; the fixture above is
  that bar met without mutating inventory.
- #135, #138 — the two changes verified here. #141 duplicates #138.

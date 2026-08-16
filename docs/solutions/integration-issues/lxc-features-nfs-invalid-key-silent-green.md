---
title: "`nfs=1` is not an LXC feature key: an invalid value, a missing `set -e`, and a snapshot-matching grep made a no-op report changed forever"
date: 2026-08-16
category: integration-issues
module: proxmox_guests
problem_type: integration_issue
component: tooling
symptoms:
  - "The `Set features for privileged LXC containers` task reports changed for CT 201 on every run, for the entire life of the deployment, and never converges"
  - "`pct config 201` still reads `features: nesting=1` after the task reports changed — the desired `nesting=1,nfs=1` never persists"
  - "`pct set 201 -features nesting=1,nfs=1` fails PVE schema validation, but the shell task exits 0 and the following `echo changed` runs anyway"
  - "The existence check greps the raw /etc/pve/lxc/201.conf, whose `[preissue77]` snapshot section carries its own `features:` line and can satisfy the match while the live config is drifted"
  - "Nothing ever surfaces the failure because NFS works regardless — the container runs with `lxc.apparmor.profile: unconfined`"
root_cause: wrong_api
resolution_type: code_fix
severity: medium
related_components:
  - proxmox
  - lxc
  - pct
  - nfs
  - ansible
tags:
  - proxmox
  - lxc
  - lxc-features
  - nfs
  - ansible
  - shell
  - silent-failure
  - idempotency
---


# `nfs=1` is not an LXC feature key: an invalid value, a missing `set -e`, and a snapshot-matching grep made a no-op report changed forever

## Problem

The task **"Set features for privileged LXC containers"** in
`ansible/roles/proxmox_guests/tasks/main.yml` reported `changed` for CT 201 on **every**
run of `ansible/playbooks/proxmox-hosts.yml`, for the entire life of the deployment, while
converging nothing. Live `pct config 201` read `features: nesting=1` throughout; the
desired `nesting=1,nfs=1` never persisted, not once.

This is not one bug. It is three independent flaws stacked so that each one hides the next:
an invalid value, a shell that swallowed the resulting error, and an existence check
reading a file that can answer the question wrong even when the first two are fixed. Remove
any one of them and the other two still produce a green report over a total no-op.

**It was never an outage, and the doc should say so plainly.** CT 201's NFS mounts to
TrueNAS over `vmbr2` worked the whole time regardless, because the container runs with
`lxc.apparmor.profile: unconfined` — set by the sibling "Disable AppArmor for privileged LXC
containers" task in the same role, and visible in `/etc/pve/lxc/201.conf`. The `mount=nfs`
feature was belt-and-braces intent that silently never landed. What was actually lost was
**signal**: a permanent `changed=1` floor on n5pro, which is the primary drift indicator
this repo's change loop gates on.

## Symptoms

- `ansible-playbook ansible/playbooks/proxmox-hosts.yml --limit n5pro` reports `changed` for
  `CT 201 (n5pro-docker) — features` on a fully converged fleet, forever. A second,
  genuinely no-op apply never reaches `changed=0`.
- The task reports `ok`/`changed`, never `failed` — so nothing in the recap suggests the
  `pct set` underneath it is erroring.
- `pct config 201` shows `features: nesting=1`. Ansible has "applied" `nesting=1,nfs=1`
  on every run for months.
- Because `changed=1` is standing noise on that host, a *real* hypervisor-level change is
  indistinguishable from it — you have to know the folklore ("that one is always like
  that") to read the recap. This is the exact failure shape #77 was about, reintroduced
  one layer down.

## What Didn't Work

- **The diagnosis in #100.** #100 ("proxmox_guests: always-changed tasks break the
  idempotency signal") reported this exact symptom and is closed as completed
  (`closedAt 2026-08-15T22:53:49Z`). It attributed the features task's always-changed
  state to the task "re-appl[ying] flags that are already set" — a plausible reading of a
  bare `ansible.builtin.shell`, and the wrong root cause. The task was not re-applying
  flags that were already set. It was **not applying anything at all**.
- **The remedy #100 suggested, taken on its own.** "Compare against the current container
  config first and skip when the flags already match" is necessary and insufficient. With
  `nfs=1` still in the desired string, the comparison can never match: `pct set` keeps
  failing, the config never converges, and absent `set -e` the task keeps echoing
  `changed`. You would have added a correct guard and changed the outcome by exactly
  nothing. Verified 2026-08-16: `git log --all --grep="#100"` returns no commits, and at
  `d56eff5` the features task still carried `set -o pipefail` (no `-e`) and `nfs=1`.
- **Grepping the container's config file to confirm the state.** The obvious check —
  `grep '^features:' /etc/pve/lxc/201.conf` — is what the task itself did, and it is
  unreliable for the reason described below. On CT 201 that file currently has **three**
  `features:` lines in three different sections.
- **Reading across from the sibling task and assuming consistency.** The
  startup/onboot reconcile tasks in the *same file*, one screen away, already carried
  `set -euo pipefail` with the inline comment
  `# -e so a failed pct set aborts instead of falsely echoing "changed"`. The lesson had
  been learned, written down, and then not carried across to its neighbour.

## Solution

### The three flaws, and what the task looked like

Before, at `d56eff5` (`ansible/roles/proxmox_guests/tasks/main.yml`, ~L333-350):

```yaml
- name: Set features for privileged LXC containers
  become: true
  ansible.builtin.shell: |
    set -o pipefail
    features="nesting={{ item.nesting | default(proxmox_lxc_defaults.nesting) | ternary('1', '0') }}"
    {% if item.nfs_enabled | default(false) %}
    features="$features,nfs=1"
    {% endif %}
    if ! grep -q "^features: $features" /etc/pve/lxc/{{ item.vmid }}.conf; then
      pct set {{ item.vmid }} -features "$features"
      echo "changed"
    fi
  register: pct_features
  changed_when: pct_features.stdout == "changed"
```

**1. `nfs` is not a valid LXC feature key.** PVE's own schema — `my $features_desc = {` at
`/usr/share/perl5/PVE/LXC/Config.pm:454` (verified live on n5pro, pve-manager version 9.2.10) —
defines exactly six keys and closes at L511:

| key | line | type |
| --- | ---- | ---- |
| `mount` | 455 | string, `format_description => 'fstype;fstype;...'` |
| `nesting` | 467 | boolean |
| `keyctl` | 476 | boolean |
| `fuse` | 488 | boolean |
| `mknod` | 495 | boolean |
| `force_rw_sys` | 504 | boolean |

There is no `nfs`. The correct spelling of "this CT may mount NFS" is `mount=nfs` — and
the `mount` description says so itself, warning that "mounting an NFS file system can block
the host's I/O completely". The schema is `additionalProperties`-closed, which you can
confirm read-only, without mutating anything:

```bash
ssh root@192.168.30.5 'perl -e "use PVE::LXC::Config;
  eval { PVE::LXC::Config->parse_features(q(nesting=1,nfs=1)) }; print \$@"'
# format error
# nfs: property is not defined in schema and the schema does not allow additional properties

ssh root@192.168.30.5 'perl -e "use PVE::LXC::Config;
  eval { PVE::LXC::Config->parse_features(q(mount=nfs,nesting=1)) }; print \$@ || qq(valid\n)"'
# valid
```

So `pct set 201 -features nesting=1,nfs=1` failed schema validation on every single run,
for the whole life of the deployment.

**2. No `set -e`.** The script set `-o pipefail` but not `-e`. `pct set` exited non-zero,
the shell carried on to the very next line, `echo "changed"` ran regardless — and
`changed_when` keyed off exactly that string. The failure had nowhere to surface: the
non-zero exit was not the script's exit status, and the marker it triggered on is emitted
by the recovery path, not the success path. **The marker reported intent, not outcome.**

**3. The existence check read the wrong artifact.** `/etc/pve/lxc/<vmid>.conf` is not the
live config. It is the live section *plus* a `[pve:pending]` section *plus* one section per
snapshot. CT 201's file today:

```
4:features: nesting=1          # live section
23:[pve:pending]
24:features: mount=nfs,nesting=1
27:[preissue77]                # snapshot taken during the #77 work
30:features: nesting=1
```

`grep -q "^features: $desired"` is anchored to line start but not to a section, so it will
happily match a **snapshot's** value and conclude the live config is converged when it is
not. This one is a latent trap independent of the other two — it would have silently
neutered even a fully correct fix.

### The fix

Current `ansible/roles/proxmox_guests/tasks/main.yml` (~L647):

```yaml
- name: Set features for privileged LXC containers
  become: true
  vars:
    # Alphabetical (mount before nesting) to match PVE's canonical print order.
    desired_features: >-
      {{ ((['mount=nfs'] if item.nfs_enabled | default(false) else [])
          + ['nesting=' ~ (item.nesting | default(proxmox_lxc_defaults.nesting) | ternary('1', '0'))]) | join(',') }}
  ansible.builtin.shell: |
    set -euo pipefail   # -e so a failed pct set aborts instead of falsely echoing "changed"
    desired="{{ desired_features }}"
    current=$(pct config {{ item.vmid }} | sed -n 's/^features: //p')

    # Order-insensitive compare: stay idempotent even if PVE ever writes the
    # keys back in a different order than we emit them.
    norm() { tr ',' '\n' | sort | paste -sd ',' -; }

    if [ "$(printf '%s' "$current" | norm)" != "$(printf '%s' "$desired" | norm)" ]; then
      pct set {{ item.vmid }} -features "$desired"
      echo "changed"
    fi
  register: pct_features
  # Last line, not whole stdout: pct/pvesh can emit progress chatter ahead of it.
  changed_when: pct_features.stdout_lines | default([]) | last | default('') == 'changed'
  loop: "{{ proxmox_lxcs }}"
  when: not (item.unprivileged | default(proxmox_lxc_defaults.unprivileged))
  loop_control:
    label: "CT {{ item.vmid }} ({{ item.hostname }}) — features"
```

Point by point:

- **`set -euo pipefail`**, with the same inline comment the sibling reconcile tasks carry,
  so the next reader sees *why* the `-e` is load-bearing rather than treating it as
  boilerplate. `/bin/sh` on these Proxmox hosts is dash (`/bin/sh -> dash`), and this dash
  **does** support `set -o pipefail` — `/bin/sh -c 'set -o pipefail && echo ok'` succeeds —
  which is why the pre-existing `-euo` tasks work on Debian at all.
- **Desired features built from a Jinja list**, emitting `mount=nfs` (when
  `item.nfs_enabled`) then `nesting=<0|1>`, **alphabetical** to match PVE's canonical print
  order so the common case compares equal on the nose.
- **Compare against `pct config`, never the file.** `pct config <vmid>` is a rendered
  view, not a section-bearing file, so a snapshot's `features:` line cannot leak into the
  comparison.
- **Order-insensitive normalised compare** via `norm()`, so the task stays idempotent even
  if PVE ever writes the keys back in an order other than the one we emit. Verified working
  under this dash.
- **`changed_when` hardened to the last stdout line** rather than the whole buffer. `pct`
  and `pvesh` can emit progress chatter, and `stdout == "changed"` is one stray line away
  from silently never firing again.

### The same defect one level up

`nfs_enabled` is only consumed by a task gated on privileged CTs, so setting it on an
unprivileged container would be silently inert — a var that looks like configuration and
does nothing. A companion assert closes that:

```yaml
- name: Assert nfs_enabled is only set on privileged containers
  ansible.builtin.assert:
    that:
      - not (item.unprivileged | default(proxmox_lxc_defaults.unprivileged))
    fail_msg: >-
      CT {{ item.vmid }} ({{ item.hostname }}) sets nfs_enabled but is
      unprivileged. nfs_enabled is only implemented for privileged CTs (the
      features task is gated on privileged); either make the CT privileged or
      drop nfs_enabled.
  loop: "{{ proxmox_lxcs | selectattr('nfs_enabled', 'defined') | selectattr('nfs_enabled') | list }}"
  loop_control:
    label: "CT {{ item.vmid }} ({{ item.hostname }}) — nfs_enabled"
```

And `ansible/inventory/host_vars/n5pro/vars.yml` now records at the declaration site what
the var maps to and why it is belt-and-braces, so nobody re-derives the mapping from the
name:

```yaml
    # `lxc.apparmor.profile: unconfined` — the feature is belt-and-braces.
    # It was previously emitted as `nfs=1`, which is not a valid feature key,
    # so it failed schema validation and never persisted (#86).
    nfs_enabled: true
```

### Verification

The comparison logic was exercised against a four-row matrix **before** any apply, because
the failure mode being fixed is precisely "the task claims it worked":

| current | desired | expected |
| ------- | ------- | -------- |
| `nesting=1` | `mount=nfs,nesting=1` | CHANGE → `pct set -features mount=nfs,nesting=1` |
| `mount=nfs,nesting=1` | `mount=nfs,nesting=1` | no-op |
| `nesting=1,mount=nfs` | `mount=nfs,nesting=1` | no-op (order-insensitive) |
| `nesting=0` | `nesting=0` | no-op |

Row 1 was CT 201's state, so exactly one `pct set` was expected. Row 2 is the second run.
Row 3 proves the reordering guard.

A `pct set` carrying the fixed value **succeeded at least once** on n5pro. Evidence, verified
read-only on 2026-08-16: `pct pending 201` reports `new features: mount=nfs,nesting=1`, and the
old `nfs=1` string is schema-invalid so it could never have produced that entry — neither the
live section nor the `[preissue77]` snapshot has ever held anything but `nesting=1`.

Do **not** read that as "the fix is now what runs against this host." `journalctl` on n5pro shows
the *pre-fix* script body (`set -o pipefail` only, `features="…,nfs=1"`, the `grep` against
`/etc/pve/lxc/201.conf`) executing against CT 201 twice *after* the successful run. Some other
checkout is still deploying this role to this host with the old code. It does no damage — a
schema-invalid `pct set` fails closed and leaves the pending value alone — but it does keep
re-creating the always-`changed` floor this document is about. Confirm no other checkout targets
the host before treating the pending state as a settled fixed-state signal.

The pending-state nuance in the next section means the operator check is three commands, not one:

```bash
ssh root@192.168.30.5 'pct config 201 | grep ^features'            # config incl. pending
ssh root@192.168.30.5 'pct config 201 --current | grep ^features'  # what the running CT has
ssh root@192.168.30.5 'pct pending 201 | grep -i feature'          # cur/new, if they differ
```

The remaining proof to collect is the idempotency one: a second apply of
`ansible/playbooks/proxmox-hosts.yml --limit n5pro` must report `changed=0` for
`CT 201 (n5pro-docker) — features`, with the `changed=1` floor gone.

## Why This Works

The three flaws failed at three different layers, which is why the bug survived so long.

**Layer 1 — the value.** `pct set` validates `-features` against a closed schema, so
`nfs=1` was a hard, immediate, correctly-reported error. PVE did its job. Nothing was
listening.

**Layer 2 — the shell.** `set -e` is what turns "a command failed" into "this task
failed". Without it, a mutate-then-echo script has two paths to the marker: the command
succeeded, or the command failed and execution fell through. Those are indistinguishable
downstream, and `changed_when` cannot tell them apart because by the time Ansible sees
`stdout` the exit status of the individual command is gone. `-e` collapses the second path
into a task failure, which is loud. This is why the fix is not "check better" but "let the
failure out" — the check is only trustworthy once errors can escape.

**Layer 3 — the read.** Every `pct`/`qm` config file in `/etc/pve` is a multi-section
document: the live config, a `[pve:pending]` block, and one block per snapshot. Line-anchored
grep has no notion of those boundaries, so it answers a question about *some* section rather
than *the live* one. `pct config` renders one view and cannot be confused by a snapshot,
which is why the comparison moves there.

**A fourth thing worth knowing: `features` is a pending-capable option.** Verified on the
running CT 201 on 2026-08-16, after the fixed task had run:

```
pct config 201            -> features: mount=nfs,nesting=1   # pending merged in
pct config 201 --current  -> features: nesting=1             # what the running CT actually has
pct pending 201           -> cur features: nesting=1
                             new features: mount=nfs,nesting=1
```

PVE cannot change a running container's feature set in place, so a successful
`pct set -features` on a running CT writes to `[pve:pending]` and takes effect at the next
stop/start. Comparing against plain `pct config` (pending merged) is the right call for the
task — re-issuing the same `pct set` would be a no-op, so the task correctly reports
converged and stays idempotent. But it means **config convergence and effective
convergence are different events**, and the role deliberately does not restart a CT hosting
the docker fleet to close the gap. Until CT 201 is next stopped and started, `mount=nfs`
is queued, not active — which is only tolerable here because AppArmor is unconfined and the
NFS mounts do not depend on it. Check `pct pending` before concluding a feature is live.

## Prevention

- **Any shell task that mutates and then echoes a marker MUST use `set -e`.** Without it
  the marker reports *intent* — "we reached the line after the command" — not *outcome*.
  This applies to the whole `mutate → echo "changed" → changed_when` idiom, which is the
  standard workaround in this repo for the privileged-LXC `403 Forbidden` API limitation, so
  the idiom is going to keep appearing. A read-only probe with `changed_when: false` (like
  "Resolve latest template for each OS" at the top of the same file) does not need `-e`;
  anything that calls `pct set`/`qm set` does.
- **Never grep `/etc/pve/*.conf` directly.** Those files carry `[pve:pending]` and
  per-snapshot sections, each with its own copy of the keys you are matching, and a
  line-anchored grep cannot tell them apart. Query the live view: `pct config <vmid>` /
  `qm config <vmid>`, plus `--current` and `pct pending <vmid>` when you need to distinguish
  configured from effective. A snapshot on a container is enough to make a correct-looking
  guard lie.
- **Validate enum-ish values against the target system's own schema before shipping them.**
  Feature keys, `ostype`, `lock` states, storage content types — these are closed
  enumerations defined in `/usr/share/perl5/PVE/**/*.pm`, and an invalid value is rejected
  at a layer your task may not be watching. A guessable-looking name (`nfs=1`) that reads
  correctly in a diff is exactly the shape that survives review. Read the schema, or probe
  it read-only with the `parse_*` helper as above — never confirm a value by writing it.
- **When a fix is written down as an inline comment in one task, grep its siblings in the
  same file for the same shape.** `# -e so a failed pct set aborts instead of falsely
  echoing "changed"` had been sitting two tasks away for the entire time this bug was live.
  A comment that explains a hazard is a search string: `grep -n 'set -o pipefail'` across the
  role would have found the one task that missed it in a second. Applying a lesson to the
  instance that prompted it and not to its neighbours is how a fixed class of bug stays
  open.
- **A var consumed by a gated task needs an assert, not a default.** `nfs_enabled` on an
  unprivileged CT would have been silently inert — the same defect one level up. When a task
  carries a `when:`, ask what happens to its inputs on the excluded branch, and fail loudly
  rather than ignore them.
- **A closed issue is evidence that someone looked, not that something changed.** #100
  described this symptom precisely and was closed as completed with no commit referencing
  it, and with a root cause that was wrong in a way that would have made its own suggested
  remedy ineffective. Before trusting a closed issue as a fix, confirm the code:
  `git log --all --grep="#<n>"` and read the task.
- **Emit multi-key config values in the target's canonical order, and compare
  order-insensitively anyway.** Canonical order makes the common path compare equal on the
  nose; the normalising compare means the task cannot start flapping if the target ever
  reorders on write. Both are cheap; only having one of them is a future always-changed task.

## Related Issues

- #86 — this fix (guest definitions are create-only; several reconcile tasks in the same role
  converge nothing).
- #100 — reported this exact symptom and is **closed as completed**, with the wrong root cause
  ("re-applying flags that are already set") and a remedy that was necessary but not sufficient.
  Verified 2026-08-16: no commit references it, and at `d56eff5` the defect was untouched.
- [[community-proxmox-update-default-blind-config-put]] — the other half of #86. Same shape,
  different mechanism: a provisioning module reported `changed` while converging nothing. Two
  independent instances in one role is why the prevention rules here are written as rules rather
  than as a note on this task.
- [[proxmox-boot-order-inversion-breaks-nfs-volume-mount]] — #36, where the shell-reconcile idiom
  this task should have copied was invented, including the `set -euo pipefail` comment that had
  been sitting two tasks away the whole time.
- [[unattended-upgrades-silently-inert-fleet-wide]] — the same silent-green shape in another
  subsystem: an artifact that exists, looks correct, and is consumed by nobody.
- [[postgresql-mounted-configs-never-deployed-or-read]] — "deploying is not wiring", and the
  general lesson that only the target system's own introspection distinguishes *configured* from
  *in effect*. `pct pending` is this role's `SHOW hba_file;`.
- [[docker-apparmor-privileged-lxc]] — why this defect had no visible consequence: CT 201 runs
  `lxc.apparmor.profile: unconfined`, so its NFS mounts never needed the feature bit.
- [[vector-057-silent-log-pipeline-failure]] — #73, the first recorded instance of the
  silent-green class in this repo.
- #119 — follow-on in the same role: the NIC and startup/onboot reconcile shells read
  `qm`/`pct config` unguarded and are invisible in check mode.
- #77 — the deployment during which the `[preissue77]` snapshot on CT 201 was taken. That
  snapshot is what makes the old `grep` on `/etc/pve/lxc/201.conf` unsafe today.

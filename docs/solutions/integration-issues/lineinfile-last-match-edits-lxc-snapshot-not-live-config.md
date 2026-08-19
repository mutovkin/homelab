---
title: "`lineinfile` rewrites the LAST match, and in an LXC conf that is the snapshot copy — plus plain `pct config` reports pending, not applied"
date: 2026-08-18
category: integration-issues
module: proxmox_guests
problem_type: integration_issue
component: tooling
symptoms:
  - "A keyed `lineinfile` (regexp + line) against /etc/pve/lxc/201.conf rewrites the duplicate `lxc.*` line inside the `[preissue77]` snapshot section, never the live copy above it"
  - "The task reports changed and fires the downstream `pct reboot`, while `pct config 201 --current` still shows the old GPU lines — the container reboots and comes back without the device it was supposed to gain"
  - "The earlier unkeyed form had the opposite failure: it appended a duplicate allow rule on every run where the derived /dev/kfd major:minor differed, never removing the stale one"
  - "A malformed derivation renders as `lxc.cgroup2.devices.allow: c  rwm` — an invalid rule with an empty device number, appended fresh every run because it can never match itself"
  - "`pct config 201` prints `features: mount=nfs,nesting=1` while `pct config 201 --current` prints `features: nesting=1` — the plain form is the pending-merged view, not the applied config"
  - "A convergence check reading plain `pct config` therefore reports converged for a value that lives only in `[pve:pending]` and is not in force"
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - proxmox
  - lxc
  - pct
  - pmxcfs
  - ansible
  - lineinfile
  - gpu-passthrough
tags:
  - proxmox
  - lxc
  - lineinfile
  - pct-config
  - snapshots
  - gpu-passthrough
  - idempotency
  - silent-failure
---

# `lineinfile` rewrites the LAST match, and in an LXC conf that is the snapshot copy — plus plain `pct config` reports pending, not applied

## Problem

`ansible/roles/proxmox_guests/tasks/lxc-gpu-passthrough.yml` manages four lines in
`/etc/pve/lxc/<vmid>.conf` on the Proxmox host to share the AMD GPU into CT 201 on n5pro:

```
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
lxc.cgroup2.devices.allow: c 511:0 rwm
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file
```

Three of those are constants. The third is not: the `/dev/kfd` allow rule embeds a
major:minor derived at runtime from the host (`stat -c '%t %T' /dev/kfd`, converted from
hex — lines 29-39). Kernel or module churn can renumber that character device, so this one
line is the only one that ever legitimately *changes*, and it is the line the reconcile
logic has to get right.

Two traps sit in that mundane job, and they compound. Both are properties of how Proxmox
stores and reports guest config, and neither announces itself.

**Trap 1 — a keyed `lineinfile` aims at the snapshot copy, not the live config.**
`/etc/pve/lxc/<id>.conf` is not a flat key/value file. It stores the live config first,
then INI-style sections — `[pve:pending]` for staged-but-unapplied changes, plus one per
snapshot — and each section carries **its own full copy** of the same `lxc.*` lines.
Verified on CT 201: the live GPU lines sit at lines 18-21, `[pve:pending]` opens at line
23, `[preissue77]` at line 27, and byte-identical duplicates of all four GPU lines appear
at lines 44-47 inside that snapshot. `ansible.builtin.lineinfile` with a `regexp:` replaces
the **last** match in the file. The last match is line 46, in the snapshot.

This is not exotic. Every guest config in this fleet carried at least one snapshot section
when this was written: CT 101 has `[preissue77]`, and VM 100 has two
(`[Pre_OS11_Upgrade]`, `[Pre_OS_11_1_Upgrade]`).

> Live-host claims in this doc (the section inventory above, and the `pct config` output
> below) were read off `pve` and `n5pro` on 2026-08-18 and cannot be re-checked from the
> repo alone. Re-verify with
> `ssh root@192.168.25.5 'grep -n "^\[" /etc/pve/lxc/101.conf /etc/pve/qemu-server/100.conf'`
> and `ssh root@192.168.30.5 'pct config 201; echo ---; pct config 201 --current'`.
> Snapshots get deleted, so treat the specific section names as of that date; the mechanism
> does not depend on them.

**Trap 2 — plain `pct config` prints the pending-merged view.**
`pct config <vmid>` without `--current` merges `[pve:pending]` over the applied config
before printing. Verified on CT 201: plain `pct config 201` shows
`features: mount=nfs,nesting=1`; `pct config 201 --current` shows `features: nesting=1` —
the value actually in force. So any drift check reading plain `pct config` can declare a
guest converged on the strength of config that is *staged and not running*. This matters
more since #86, because a blind config PUT against a running CT is precisely what leaves a
`[pve:pending]` section behind.

The combination is what makes this dangerous rather than untidy: an idempotency check that
reads the wrong view, feeding an editor that writes to the wrong region, in a task whose
downstream action is `pct reboot` on a live container.

## Symptoms

- The original unkeyed `lineinfile` appended a **duplicate** `lxc.cgroup2.devices.allow`
  line on every run where the derived `/dev/kfd` major:minor differed from the one on disk —
  the stale rule was never removed, and each run reported `changed` and rebooted the container.
- With a `regexp:` key added (the obvious fix), a `/dev/kfd` renumber would rewrite
  **line 46 — inside the `[preissue77]` snapshot** — report `changed`, fire `pct reboot`,
  and leave the live rule at line 20 stale. Net effect: a fully green play that silently
  removes GPU passthrough from a running container *and* corrupts a snapshot's stored config.
- A malformed derivation (`stat` fails, `/dev/kfd` disappears mid-run) rendered as
  `lxc.cgroup2.devices.allow: c  rwm` — an invalid cgroup rule with an empty device number,
  appended fresh every run because it could never match its own key.
- Any convergence check against plain `pct config` reports "already converged" for a value
  that lives only in `[pve:pending]` and is not in force in the running container.
- `--check` runs produced no useful verdict at all: the read-only derivation task was
  skipped in check mode, so the dry run diffed against an empty device number.

## What Didn't Work

**Attempt 1 — unkeyed `lineinfile` (the original).** Each managed line was passed as
`line:` with no `regexp:`:

```yaml
- name: "Add GPU KFD passthrough to LXC config — CT {{ item.vmid }}"
  ansible.builtin.lineinfile:
    path: "/etc/pve/lxc/{{ item.vmid }}.conf"
    line: "{{ kfd_line }}"
  loop:
    - "lxc.cgroup2.devices.allow: c {{ dev_kfd_major_minor.stdout | default('') }} rwm"
    - "lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file"
```

Without a key, `lineinfile` matches on the whole literal line. A renumbered `/dev/kfd`
doesn't match the old line, so the new rule is *appended* and the stale one stays. The
container accumulates allow rules; every run reboots it.

**Attempt 2 — add a `regexp:` key.** This is what issue #94 item 5 actually asked for
("key the line with a regexp/marker"), and it is the natural correction:

```yaml
  loop:
    - line: "lxc.cgroup2.devices.allow: c {{ dev_kfd_major_minor.stdout | default('') }} rwm"
      regexp: '^lxc\.cgroup2\.devices\.allow: c \d+:\d+ rwm$'
    - line: "lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file"
      regexp: '^lxc\.mount\.entry: /dev/kfd dev/kfd '
```

**It is strictly worse than the bug it fixes**, and this doc exists mostly to record that.
The duplicate-append is loud and harmless — a human reading `pct config` sees two allow
rules. The keyed version is silent and harmful: `lineinfile`'s last-match semantics point
the write at the snapshot copy, so the live config never changes while Ansible reports
success and reboots the container. The progression matters more than either endpoint: the
fix that "obviously" restores idempotency is the one that turns a visible mess into an
invisible outage, because the tool's matching semantics and the file's layout interact in a
way neither the module docs nor the file's appearance advertise. **Treat #94 item 5 as
superseded by this doc.**

**Attempt 3 — grep the raw config file to decide convergence.** Rejected for the same
reason the `features` task was fixed in #86: snapshot sections carry their own copies of
every key, so a raw grep false-matches a snapshot and skips a genuinely drifted live
config. `pct config` is the right tool — but only in its `--current` form (Trap 2).

## Solution

The file now derives drift from a read-only probe of the **applied** config, prints an
explicit verdict, and mutates only the region before the first section header — and only
when it can prove what it is deleting.

**1. Assert the runtime-derived value before it can be written** (line 44). A blank or
malformed major:minor fails the play instead of landing in a live container's config:

```yaml
- name: "Assert /dev/kfd major:minor is well-formed — CT {{ item.vmid }}"
  ansible.builtin.assert:
    that:
      - dev_kfd_major_minor.stdout | default('') is match('^\d+:\d+$')
```

**2. Probe the applied config, read-only, in check mode too** (line 69):

```yaml
- name: "Read applied GPU passthrough config — CT {{ item.vmid }}"
  ansible.builtin.command: "pct config {{ item.vmid }} --current"
  register: lxc_gpu_live
  changed_when: false
  # Read-only, so --check must run it or the drift verdict is derived from nothing.
  check_mode: false
```

**3. Compute two sets — `missing` and `extras`** (lines 87-95). `missing` is the desired
lines absent from the applied config; `extras` is every numeric cgroup allow rule present
but not desired:

```yaml
    lxc_gpu_missing: "{{ _desired | difference(lxc_gpu_live.stdout_lines) }}"
    lxc_gpu_extras: >-
      {{ lxc_gpu_live.stdout_lines
         | select('match', '^lxc\.cgroup2\.devices\.allow: c [0-9]+:[0-9]+ rwm$')
         | difference(_desired) }}
```

**4. State the verdict out loud** (line 97), so a `--check` run says *converged* or
*WOULD CHANGE — missing: […] extras: […]* rather than leaving the operator to infer it from
a skipped task.

**5. Mutate with a drift-gated bash task** (line 106 onward, gated on `missing` or `extras`
being non-empty). Its guarantees, in order:

- **Refuse rather than guess** (lines 111-125). More than one extra → exit 1: only one can
  be the stale `/dev/kfd` rule and the rest may be deliberate. An extra alongside an
  otherwise-complete desired set → exit 1: nothing is stale, so that rule is somebody's
  intentional passthrough.
- **Split at the first section header and edit only what precedes it** (lines 132-139).
  Everything from `^\[` onward is copied through byte-for-byte:

  ```bash
  if grep -q '^\[' "$conf"; then
    hdr=$(grep -n -m1 '^\[' "$conf" | cut -d: -f1)
    live_lines=$((hdr - 1))
  else
    live_lines=$(wc -l < "$conf")
  fi
  head -n "$live_lines" "$conf" > "$work/live"
  tail -n +$((live_lines + 1)) "$conf" > "$work/rest"
  ```

- **Delete by fixed string, never by shape** (line 145). The one stale rule is removed with
  `grep -vxF` against the literal text the probe read back.
- **Append only what is genuinely absent**, with `grep -qxF`.
- **Structural sanity gate** (line 170): if `^arch:` is missing from the merged output, the
  split or a filter ate real content — refuse to write.
- **Timestamped backup** to `/var/backups/lxc-<vmid>.conf.<epoch>` (lines 175-176).
- **Write by truncation**: `cat "$work/merged" > "$conf"`. `/etc/pve` is pmxcfs, a FUSE
  filesystem; `mv`/rename onto it is not safe.
- **Post-verify against the applied view**: every desired line must appear in
  `pct config --current`, and the stale rule must be gone, or the task fails.

**6. Reboot only on the mutator's real result** (line 212): `when: lxc_gpu_reconcile.changed | default(false)`.

One packaging note that cost time: `ansible-lint`'s argument splitter cannot parse Jinja
`{% %}` blocks inside a freeform `ansible.builtin.shell: |` (it emits `parser-error:
failed at splitting arguments`). The task uses the `shell:` → `cmd:` sub-key form for
exactly this reason — and so, it turns out, does the only other shell task in this role
that templates a `{% for %}` loop, the VM NIC reconcile at
`ansible/roles/proxmox_guests/tasks/main.yml:386`. The four freeform `shell: |` tasks
alongside it template expressions only, never control flow. The rule is therefore already
observed in the role; it just was not written down.

## Why This Works

**The probe reads what is in force.** `pct config --current` prints the applied config with
no pending merge, and — like plain `pct config` — never prints snapshot sections. That makes
it the only view where "converged" means "running this way right now." The drift computation
and the post-verify both read through it, so the task's notion of state and reality cannot
diverge.

**The write is region-scoped rather than pattern-scoped.** Instead of asking a matcher to
find the right line in a file with several plausible candidates, the mutator establishes the
boundary structurally — the first `^\[` header — and edits only before it. Snapshot content
is passed through untouched by construction, not by hoping a regex misses it. `lineinfile`'s
last-match rule is no longer something the task has to reason about, because `lineinfile` is
no longer in the picture.

**Deletion is grounded in an observation, not an inference.** This is the subtlest part. The
regex `^lxc\.cgroup2\.devices\.allow: c [0-9]+:[0-9]+ rwm$` is a good *detector* for "numeric
allow rule that isn't one of mine" — and a terrible *deletion predicate*, because a Coral TPU
(`c 189:0 rwm`) and `/dev/net/tun` (`c 10:200 rwm`) match it exactly. A shape-based delete
would have quietly removed a human's deliberate device passthrough as collateral damage on an
unrelated GPU renumber. So the shape is used only to *count and report*; the actual removal is
`grep -vxF` on the one literal string the probe returned, and only when exactly one extra
exists **and** something is missing — the only configuration that is unambiguously "the old
value of the line I am about to write." Every other combination exits 1 and asks a human.

**Failure modes are loud and recoverable.** The malformed-value assert catches bad input
before it reaches disk; the `arch:` check catches a bad transform before it reaches disk; the
backup catches everything else after it does. And because the reboot keys off the mutator's
`changed`, no container is bounced for an edit that didn't land.

## Prevention

- **`lineinfile` + `regexp:` replaces the LAST match.** Before keying a line in any file that
  may contain repeated or sectioned content, ask which occurrence the module will pick.
  `/etc/pve/lxc/<id>.conf` and `/etc/pve/qemu-server/<id>.conf` are disqualified outright —
  every guest in this fleet has snapshot sections today. The same applies to `replace` and
  `blockinfile`. This widens the existing "never grep `/etc/pve/*.conf`" rule from reads to
  writes, which is the more dangerous half: a bad read skips a change, a bad write corrupts a
  snapshot *and* triggers a reboot.
- **Prefer `pct set` / `qm set` over editing the file at all.** The `features` task
  (`ansible/roles/proxmox_guests/tasks/main.yml`) reconciles through `pct set` and never touches the conf
  file — that is the better pattern where PVE exposes an option for the key. File editing is
  justified here only because raw `lxc.*` keys have no `pct set` equivalent. Reach for the
  file last, not first.
- **Read Proxmox guest state with `pct config <vmid> --current` / `qm config <vmid> --current`
  for any "is it applied?" question.** Plain output merges `[pve:pending]`, so it reports
  staged config as if in force. Plain `pct config` is still the right question for "would
  re-issuing `pct set` be a no-op?" — the two forms answer different questions, and neither is
  "the live view."
- **Detect by shape; delete by literal.** A regex that identifies a *class* of line is not a
  licence to remove members of that class. Remove only a specific string that a read-only probe
  observed on this run, and only when the surrounding state makes its staleness unambiguous.
  When it doesn't, exit non-zero — refusing to guess is a valid outcome for a reconcile task,
  and a much better one than deleting somebody's Coral TPU rule.
- **Every config mutator on a live guest needs the same four fixtures:** a drift gate so it only
  runs when there is work, a structural assert on the output (`^arch:` here) before the write, a
  timestamped backup, and a post-verify through the same authoritative view the drift check
  used. A mutator that reports `changed` without re-reading state is asserting, not verifying.
- **Write to `/etc/pve` by truncation.** It is pmxcfs; `mv`/rename semantics are not reliable
  there, which also rules out Ansible's own atomic-replace modules for these files.
- **Make read-only probes `check_mode: false`.** A `--check` verdict derived from a skipped
  probe is worse than no verdict — the original file's dry run diffed against an empty device
  number and reported a permanent phantom change.
- **`ansible-lint` cannot parse Jinja `{% %}` inside a freeform `ansible.builtin.shell: |`.**
  Use the `shell:` → `cmd:` sub-key form whenever a shell body templates control flow.

## The sibling that was missed

The first pass migrated only `lxc-gpu-passthrough.yml`. The same role carried a second
`lineinfile` against the same files — *"Disable AppArmor for privileged LXC containers
(required for Docker)"*, keyed `^lxc\.apparmor\.profile:` — and it is a strictly worse
instance of the trap, because its failure is not "the CT loses a GPU" but "Docker will not
start at all": the live section stays confined and `apparmor_parser: Access denied` comes
back. Its guest set is `unprivileged: false`, which today is CT 201 alone — the very
container whose `[preissue77]` / `[pve:pending]` sections make the last-match rewrite real.

It is now `lxc-apparmor-unconfined.yml`, with the same six fixtures. Two deliberate
differences from the GPU file:

- **Not folded into the GPU reconcile's `desired` array.** That file loops over
  `gpu_sharing: true` guests; this one over `unprivileged: false` guests. They coincide on
  CT 201 today, and folding would have silently meant that a future privileged CT without a
  GPU never gets unconfined — the same "applies to a different guest set" bug in a new place.
- **No `pct reboot`.** `lxc.apparmor.profile` is single-valued, so a stale line is
  unambiguously replaced rather than counted as a possibly-deliberate extra; but it is read
  at container *start*, and the task runs immediately before "Start LXC containers". A
  stopped CT picks it up there; for a running one the task prints an explicit
  "still confined until `pct reboot`" warning instead of bouncing the fleet's Docker host
  mid-play.

**Generalised rule:** when a pattern is disqualified, grep the whole repo for it in the same
change. `grep -rn "lineinfile" ansible/roles/proxmox_guests/` was a five-second check that
would have caught this a batch earlier.

## Related Issues

- #98 — GPU passthrough keying; this fix. #94 — the correctness batch it rode in on. Item 5 of
  #94 proposed the regexp keying that Attempt 2 shows to be harmful; consider it superseded.
- [`lxc-features-nfs-invalid-key-silent-green.md`](lxc-features-nfs-invalid-key-silent-green.md) —
  the closest sibling and the read-side half of trap 1 (a grep matched a snapshot's `features:`
  line). It already records the `--current` distinction in passing, but its prose calls plain
  `pct config` "a rendered view … cannot be confused by a snapshot" and its prevention rule
  scopes the hazard to grep. Both want a refresh: `pct config` is snapshot-proof *and*
  pending-merged, and the hazard extends to line-editing writes.
- [`community-proxmox-update-default-blind-config-put.md`](community-proxmox-update-default-blind-config-put.md) —
  why this role owns explicit reconcile tasks at all, and why a blind PUT against a running CT
  leaves the `[pve:pending]` section that makes trap 2 bite.
- [`proxmox-boot-order-inversion-breaks-nfs-volume-mount.md`](../runtime-errors/proxmox-boot-order-inversion-breaks-nfs-volume-mount.md) —
  origin of the shell-reconcile idiom used here; its snippet reads plain `qm config`, so it
  inherits trap 2 and is a refresh candidate.
- Prior sessions (session history) established "writes to a running CT can land as pending" during
  #86, but never examined `pct config` vs `--current`. Trap 2 is the missing half of that finding.

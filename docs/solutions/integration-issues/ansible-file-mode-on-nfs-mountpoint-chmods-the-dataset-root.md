---
title: "An Ansible `file: mode:` on an NFS mountpoint directory chmods the NAS DATASET ROOT once the share is mounted — the placeholder and the share are the same path"
date: 2026-09-02
category: integration-issues
module: proxmox_host
problem_type: integration_issue
component: tooling
symptoms:
  - "The mountpoint `file: state: directory, mode: 0755` task reported `changed` on the SECOND apply — after the hookscript had mounted the share there — and `ok` on the third"
  - "`stat /mnt/nfs/music` on the host read `755` while the dataset's own subdirectories were `777`; the NAS dataset root had been rewritten by the hypervisor's config-management run"
  - "No Ansible task, `--check` diff, or recap named the NAS: the diff showed a local path getting the mode it was told to have"
root_cause: config_error
resolution_type: code_fix
severity: high
related_components:
  - proxmox
  - nfs
  - truenas
  - ansible
tags:
  - ansible
  - nfs
  - mountpoint
  - file-module
  - chmod
  - data-integrity
  - proxmox
  - silent-failure
---

# An Ansible `file: mode:` on an NFS mountpoint directory chmods the NAS DATASET ROOT once the share is mounted — the placeholder and the share are the same path

## Problem

`roles/proxmox_host` (#254) creates the host-side mountpoint for an NFS dataset that
is later bind-mounted into an unprivileged CT. The first version used the obvious
task — `ansible.builtin.file: path: /mnt/nfs/music, state: directory, mode: "0755"`.
On the first apply nothing was mounted there, so it created a local placeholder.
The pre-start hookscript then mounted the TrueNAS dataset onto that path. On the
**second** apply the same task found "a directory with the wrong mode" at the same
path and fixed it — on the NAS. The dataset root `/mnt/vault/media/music` on TrueNAS
now had mode 755; its previous mode is unknown, because nothing recorded it.

## Symptoms

- Second apply: `changed: [n5pro] => (item=/mnt/nfs/music)` from the mountpoint task,
  after a first apply that had already created the directory. Third apply: `ok`.
  A reconcile task that changes on the second run and settles on the third is the
  tell that something else rewrote its target in between — here, a mount.
- `stat -c '%a' /mnt/nfs/music` on the host → `755`, while `ls -ln /mnt/nfs/music`
  showed the dataset's child directories at `777` with `3000:3000` ownership.
- The mode write succeeded because the TrueNAS export maps every client uid to the
  dataset owner (Mapall) — the hypervisor's root became the owner and was allowed
  to chmod.

## What Didn't Work

- **Reading the `--check --diff` output.** It showed a local path acquiring `0755`,
  exactly what the task declares; nothing indicated the path was a live NFS mount.
- **Trusting `changed` as convergence.** The task was "converging" the share root
  toward a mode the repo had never meant to manage.

## Solution

Probe first; create the placeholder only while nothing is mounted there; never put a
`mode:` on a path that can become a mount
(`ansible/roles/proxmox_host/tasks/nfs-mounts.yml`):

```yaml
- name: Probe whether NFS mountpoints are currently mounted
  ansible.builtin.command:
    argv: [mountpoint, -q, "{{ item.path }}"]
  register: proxmox_nfs_mountpoint_probe
  changed_when: false
  # util-linux 2.41 (measured): 0 = mounted, 1 = path missing, 32 = not a mountpoint
  failed_when: proxmox_nfs_mountpoint_probe.rc not in [0, 1, 32]
  check_mode: false
  loop: "{{ proxmox_nfs_mounts }}"

- name: Ensure NFS mountpoint placeholder directories exist (unmounted paths only)
  ansible.builtin.file:
    path: "{{ item.item.path }}"
    state: directory
    mode: "0755" # safe: this task never runs on a mounted path
  loop: "{{ proxmox_nfs_mountpoint_probe.results }}"
  when: item.rc != 0
```

The dataset root's mode was not restored by this change: its prior value was never
measured, so the operator was told and left to set it on TrueNAS (Datasets → Edit
Permissions) if other clients need more than owner-write at the root.

## Why This Works

A mountpoint directory has two lives at one path: an empty local placeholder before
the mount, and the remote filesystem's root after it. `ansible.builtin.file` sees
only "a directory at this path" and applies the declared mode to whichever one is
there. Gating on `mountpoint -q` makes the task act only on the placeholder; dropping
any ambition to manage the mode of a mounted path removes the write entirely. The
same trap applies to `owner:`/`group:` and to `recurse:`, which would have been far
worse.

## Prevention

- **Never declare `mode`/`owner`/`group` on a path that a mount can land on** — a
  mountpoint, a bind-mount source, a Docker volume path. Create it bare, or probe
  `mountpoint -q` first as above.
- **A task that changes on the second run and not the first is evidence, not
  noise** (the repo's idempotency rule): ask what happened to the target between the
  two runs before accepting the settle.
- **Record the pre-state before pointing configuration management at shared
  storage** — one `stat` line in the PR would have made the rollback exact.
- Mapall exports mean the hypervisor's root writes as the data owner: every host-side
  task that touches `/mnt/nfs/**` is a write to the NAS with the owner's permissions.

## Related Issues

- #254 — the change that introduced and fixed this (branch `feat/254-workbench-lxc`,
  pending merge as of this writing).
- [lxc-systemd-259-needs-nesting-credentials-243.md](lxc-systemd-259-needs-nesting-credentials-243.md)
  — sibling lesson from the same CT bring-up.
- [docs/n5pro.md — Workbench CTs](../../n5pro.md#workbench-cts) — the host-bind-mount
  pattern this mountpoint serves.

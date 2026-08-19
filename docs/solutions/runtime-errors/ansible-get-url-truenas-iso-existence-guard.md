---
title: "Ansible get_url fails on pulled TrueNAS BETA ISO; guard with stat existence check"
date: 2026-06-20
category: runtime-errors
superseded_by: docs/solutions/conventions/ansible-change-loop-pitfalls.md
module: proxmox_guests
problem_type: runtime_error
component: tooling
symptoms:
  - "Task 'Download TrueNAS ISO if missing' fails with '[Errno 111] Connection refused'"
  - "get_url fails even when the ISO already exists on the Proxmox host"
  - "task deploy:full aborts during Proxmox guest provisioning"
root_cause: missing_workflow_step
resolution_type: config_change
severity: high
related_components:
  - proxmox
  - truenas
tags:
  - ansible
  - proxmox
  - truenas
  - get-url
  - idempotency
  - version-pinning
---

# Ansible get_url fails on pulled TrueNAS BETA ISO; guard with stat existence check

> **Superseded (#7 lineage, #103).** The stat-exists gate this doc prescribes was
> later found to defeat `get_url`'s checksum verification and was removed. Current
> convention: checksum-driven `get_url`, no existence gate — see
> [ansible-change-loop-pitfalls.md §2](../conventions/ansible-change-loop-pitfalls.md#2-an-existence-gate-can-defeat-the-verification-it-precedes).
> The diagnosis below (why a dead upstream URL breaks an already-converged host)
> is still accurate and worth reading; only the prescribed fix changed.

## Problem

`task deploy:full` aborted during Proxmox guest provisioning because the
`proxmox_guests` role hardcoded the TrueNAS ISO at `26.0.0-BETA.1`, which was
removed upstream after being superseded by `26.0.0-BETA.2`. This blocked any
infrastructure change that runs through the standard playbook.

## Symptoms

- Task `Download TrueNAS ISO if missing` (`ansible.builtin.get_url`) fails the play.
- `Request failed: <urlopen error [Errno 111] Connection refused>` fetching
  `https://iso.sys.truenas.net/TrueNAS-26-BETA/26.0.0-BETA.1/TrueNAS-26.0.0-BETA.1.iso`.
- The failure occurs **even when the ISO already exists** on the Proxmox host.

## What Didn't Work

- **Re-running `task deploy:full`** produced the identical `Connection refused`
  failure every time — nothing in run state changed, the URL still pointed at the
  removed BETA.1 artifact.
- **Trusting the existing `when:` condition.** The original task gated only on
  whether a `truenas` VM was defined:
  ```yaml
  when: >
    proxmox_vms | selectattr('name', 'equalto', 'truenas') | list | length > 0
  ```
  This looks like "only act when needed," but it never checks whether the ISO is
  already on disk. `get_url` still makes a network call to the dead URL to
  validate the remote resource, so the task fails regardless of local state.

## Solution

`ansible/roles/proxmox_guests/tasks/main.yml` — add an explicit `stat` check and
gate the download on file absence; bump the version to BETA.2.

**Before:**
```yaml
- name: Download TrueNAS ISO if missing
  ansible.builtin.get_url:
    url: "https://iso.sys.truenas.net/TrueNAS-26-BETA/26.0.0-BETA.1/TrueNAS-26.0.0-BETA.1.iso"
    dest: "/var/lib/vz/template/iso/TrueNAS-26.0.0-BETA.1.iso"
    mode: "0644"
  when: >
    proxmox_vms | selectattr('name', 'equalto', 'truenas') | list | length > 0
```

**After:**
```yaml
- name: Check if TrueNAS ISO exists
  ansible.builtin.stat:
    path: "/var/lib/vz/template/iso/TrueNAS-26.0.0-BETA.2.iso"
  register: truenas_iso_stat
  when: >
    proxmox_vms | selectattr('name', 'equalto', 'truenas') | list | length > 0

- name: Download TrueNAS ISO if missing
  ansible.builtin.get_url:
    url: "https://iso.sys.truenas.net/TrueNAS-26-BETA/26.0.0-BETA.2/TrueNAS-26.0.0-BETA.2.iso"
    dest: "/var/lib/vz/template/iso/TrueNAS-26.0.0-BETA.2.iso"
    mode: "0644"
  when: >
    proxmox_vms | selectattr('name', 'equalto', 'truenas') | list | length > 0
    and not truenas_iso_stat.stat.exists | default(false)
```

Companion change — `ansible/inventory/host_vars/n5pro/vars.yml` (VM 200, `ide2`):
```yaml
ide2: "local:iso/TrueNAS-26.0.0-BETA.2.iso,media=cdrom"
```
keeps the VM's mounted cdrom filename in sync with the downloaded ISO. Committed in `1049e7b`.

## Why This Works

`ansible.builtin.get_url` is not a pure "download if absent" operation. To decide
whether the destination is up to date, it contacts the server before skipping.
When the URL points to a removed artifact, that validation request fails with
`Connection refused` — even if a valid local copy exists. The original `when:`
guarded only on VM definition, so the network call always happened.

The fix adds an `ansible.builtin.stat` that registers `truenas_iso_stat`, and the
`get_url` now also requires `not truenas_iso_stat.stat.exists | default(false)`.
When the ISO is already on disk the download task is skipped entirely — no HTTP
request is made — so a dead or changed upstream URL cannot break an otherwise
converged host. The `| default(false)` keeps the expression safe when `stat` was
itself skipped (e.g. no truenas VM defined).

## Prevention

- ~~**Guard large/remote downloads with an explicit `stat`**~~ — **superseded, do not
  do this.** Give `get_url` a `checksum:` and NO existence gate: the module hashes
  the on-disk file, skips the fetch when it matches, re-downloads on mismatch, and
  fails loudly if it is still bad. The gate prescribed in Solution above trades a
  dead-URL failure for a worse one — a corrupt local file is trusted forever,
  because verification never runs. See the banner at the top of this doc.
- **Pin the version in one place.** The version string appears in the stat path,
  the get_url url/dest, and the VM `ide2` reference. Extract one variable
  (e.g. `truenas_iso_version`) and interpolate it everywhere so a bump is a
  one-line change that cannot drift.
- **Bump beta/rolling versions deliberately.** BETA channels remove old artifacts,
  so a hardcoded URL is a time bomb — track upstream and update the pinned var
  intentionally.

## Related Issues

- [Docker AppArmor failure in privileged LXC](../integration-issues/docker-apparmor-privileged-lxc.md) — surfaced in the same `deploy:full` run.

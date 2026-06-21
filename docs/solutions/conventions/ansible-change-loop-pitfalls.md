---
title: "Ansible change-loop pitfalls: check-mode safety, idempotency gates, first-run ordering"
date: 2026-06-20
category: conventions
module: ansible
problem_type: convention
component: tooling
severity: medium
applies_when:
  - "writing or reviewing any Ansible role/task in this repo"
  - "a task registers stdout from a command/shell/pvesh/pveam lookup"
  - "guarding a download or an action behind an existence/stat check"
  - "adding a service that must start on a fresh host or bind to the network"
  - "using no_log on a task to hide a secret"
related_components:
  - proxmox_guests
  - proxmox_host
  - docker_host
  - nut
tags:
  - ansible
  - check-mode
  - idempotency
  - no-log
  - first-run
  - review-loop
---

# Ansible change-loop pitfalls

## Context

A 10-issue review-fix-deploy series against the live Proxmox hosts (issues #4–#15,
#20) surfaced the same few classes of mistake repeatedly. They are easy to write,
pass a casual read, and only reveal themselves on a fresh host, a second run, or
under `--check`. This doc is the checklist to apply *before* a fix reaches review,
so the next iteration of the [[homelab-change-loop]] doesn't relearn them. See
also `.claude/skills/homelab-change-loop/SKILL.md`.

## Guidance

### 1. Make tasks check-mode safe (so `--check` actually works)

`--check` **skips** `ansible.builtin.command`/`shell` tasks, so anything that
parses their registered `stdout` gets an empty string and blows up — which means
the dry-run-before-apply workflow itself fails. Two fixes:

- For a **read-only** lookup whose result is needed in check mode, run it anyway:
  ```yaml
  - name: Resolve latest template for each OS
    ansible.builtin.shell: "pveam available ..."
    register: template_resolution
    changed_when: false
    check_mode: false   # read-only; must run in --check so downstream facts populate
  ```
- For a parse that can legitimately see empty stdout, default the *empty string*,
  not just undefined — pass `true` as the second arg:
  ```yaml
  _cfg: "{{ some_json.stdout | default('{}', true) | from_json }}"
  monitor: "{{ detect.stdout | default('', true) | trim or 'fallback-value' }}"
  ```
  Plain `default('{}')` does **not** trigger on `''` (only on undefined).

A `--check`-only failure is still a real defect: the documented preview workflow
must complete `failed=0` on every host.

### 2. An existence gate can defeat the verification it precedes

Gating a verifying action behind "does the file exist?" skips verification of an
existing-but-bad file. `get_url` with a `checksum:` is already idempotent — it
hashes the on-disk file, skips the fetch when it matches, re-downloads on
mismatch, fails loudly if still bad. A `when: not stat.exists` gate in front of it
means a corrupt file already on disk is trusted forever.

```yaml
# WRONG — corrupt existing file is never re-verified:
- stat: { path: "{{ dest }}" }
  register: iso
- get_url: { url: "...", dest: "{{ dest }}", checksum: "sha256:{{ sum }}" }
  when: not iso.stat.exists

# RIGHT — verification runs every converge; idempotent by checksum:
- get_url: { url: "...", dest: "{{ dest }}", checksum: "sha256:{{ sum }}" }
```
Let the idempotent module own "skip if already good"; don't short-circuit it.

### 3. `no_log` also censors failure output

`no_log: true` hides the whole task result — including the module's `fail_json`
message on failure. Necessary on secret-bearing tasks, but it makes a failure
opaque. Document the escape hatch in a comment: re-run with `ANSIBLE_NO_LOG=False`
on a trusted terminal to see the error (it re-exposes the secret).

### 4. First-run ordering: install/disable before the thing that uses it

Tasks that pass on a re-run can fail on a *fresh* host because a prerequisite ran
too late:

- **Detect after install.** Detecting a systemd unit name before the package is
  installed yields the wrong fallback — the unit files don't exist yet. Install
  the base package first, then detect.
- **Mask AppArmor before installing docker-ce.** The `docker-ce` postinst
  auto-starts dockerd immediately, so in a privileged LXC the AppArmor mask must
  run *before* the package install, not after, or the first daemon start races
  AppArmor.

### 5. Bind to specific addresses, keep the paths that must work

Don't bind a service to `0.0.0.0`. Enumerate the addresses that actually need it —
including `127.0.0.1` for any local client. NUT's upsd serves both n5pro's own
upsmon (`@localhost`) and the eq12 client over the LAN, so it binds
`[127.0.0.1, 192.168.30.5]`, not all interfaces. Default the list to loopback so a
host that forgets fails closed (local works, nothing exposed).

## Why This Matters

Every one of these passes a quick read and a normal re-run. They surface only on a
fresh provision, a second converge, or a `--check` preview — exactly the moments
you most need the playbook to behave. Catching them by checklist before review is
far cheaper than a failed live provision or a silently-trusted bad artifact.

## When to Apply

Use as a pre-review checklist for any Ansible change in this repo; especially when
a task registers command output, guards an action behind a stat/existence check,
starts a service on first boot, binds to the network, or hides a secret.

## Examples

- Check-mode fix (#20): `check_mode: false` on `pveam` lookups so
  `proxmox-hosts.yml --check` reaches `failed=0` on both hosts.
- Idempotency-gate fix (#7): removed the `stat` gate so `get_url`'s sha256 verifies
  the on-disk TrueNAS ISO every converge — supersedes the existence-only guard in
  [[ansible-get-url-truenas-iso-existence-guard]].
- Ordering fixes (#4 nut detect-after-install, #5 mask-AppArmor-before-docker-ce).
- Bind fix (#9): upsd `listen_addresses: [127.0.0.1, 192.168.30.5]`.

## Related

- `.claude/skills/homelab-change-loop/SKILL.md` — the loop this checklist feeds.
- [[ansible-get-url-truenas-iso-existence-guard]] — partially superseded by #7.
- [[docker-apparmor-privileged-lxc]] — the AppArmor-in-LXC background for #5.

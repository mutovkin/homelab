---
title: "Putting a Homebrew prefix ahead of /usr/bin on PATH silently moved Ansible's discovered interpreter to brew's python — and three sibling traps from hand-rolling Homebrew on Linux in a CT"
date: 2026-09-05
category: integration-issues
module: linuxbrew
problem_type: integration_issue
component: tooling
symptoms:
  - "`[WARNING]: Host 'music_workbench' is using the discovered Python interpreter at '/home/linuxbrew/.linuxbrew/bin/python3.14'` on the run AFTER the one that installed brew; the fresh-build run had used /usr/bin/python3"
  - "`brew list --versions` prints the full list and then exits 1: `Error: No such file or directory @ dir_initialize - /home/linuxbrew/.linuxbrew/Caskroom`"
  - "`Error: The current working directory must be readable to linuxbrew to run brew.` from a `runuser -u linuxbrew -- brew` issued by a root ssh session (cwd /root, 0700)"
  - "`Error: Running Homebrew as root is extremely dangerous and no longer supported.` from a root shell whose `brew` resolved to the prefix's own binary instead of the /usr/local/bin wrapper"
  - "`apt-get -s purge libchromaprint-tools` listed `picard` for removal — a package no host_vars list declared, left behind by an earlier apply because `common` only ever adds packages"
root_cause: config_error
resolution_type: code_fix
severity: high
related_components:
  - ansible
  - proxmox
  - nfs
  - apt
tags:
  - ansible
  - homebrew
  - linuxbrew
  - path
  - interpreter-discovery
  - apt
  - nfs
  - findmnt
  - workbench
  - silent-failure
---

# Putting a Homebrew prefix ahead of /usr/bin on PATH silently moved Ansible's discovered interpreter to brew's python — and three sibling traps from hand-rolling Homebrew on Linux in a CT

## Problem

#258 gave the music workbench (CT 202, unprivileged Ubuntu 26.04) upstream-current
audio tools through Homebrew on Linux (`roles/linuxbrew`). To make a brew tool win
over a forgotten apt copy for every session type, the role's FIRST revision wrote
the prefix into `/etc/environment`'s `PATH` ahead of `/usr/bin`. Several brew
formulae depend on `python@3`, so after the first apply `python3` on PATH was
brew's 3.14.7.

Ubuntu 26.04 is not in this ansible-core's distro→interpreter map, so
`interpreter_python: auto` falls back to resolving a bare `python3` from PATH. The
result: the fresh-build run used `/usr/bin/python3`, every later run used
`/home/linuxbrew/.linuxbrew/bin/python3.14` — an interpreter with no `python3-apt`
bindings. `ansible.builtin.apt` survived only because it respawns itself; anything
without a respawn path would have broken. The play stayed green throughout, and the
implementing session had written "known and accepted: Ansible uses /usr/bin/python3
by discovery" into the review brief. The reviewer measured it false.

## Symptoms

- The interpreter warning above, on the second run only.
- `brew list --versions` exiting 1 after printing the list.
- Root's `brew` reaching Homebrew's own binary and refusing.
- brew refusing to start from a root session's cwd.
- An apt purge that would have taken an undeclared package with it.

## What Didn't Work

- **"Ansible discovers /usr/bin/python3, so PATH order does not matter."** It does
  not on this distro release; discovery is PATH-dependent whenever the distro map
  has no entry. A claim about discovery is a claim to measure (`ansible <host> -m
  ping` prints `discovered_interpreter_python`).
- **`brew shellenv` in `/etc/profile.d` for PATH.** Non-login sessions — `ssh host
  cmd`, rsync-over-ssh, Ansible tasks — never read `profile.d`; only pam_env's
  `/etc/environment` reaches them. And `shellenv` re-prepends the prefix, which
  defeats the wrapper ordering below.
- **Prefix FIRST on PATH.** Root's `brew` then resolves to the prefix's own binary,
  which refuses to run as root — and, as above, 135 system binaries move to brew.
  The order that works is `/usr/local/bin` (root wrapper) → system directories →
  prefix; "a brew tool wins" comes from the purge, not from PATH order.
- **`mountpoint -q` as the "is this the NAS" gate** for creating a directory on a
  bind-mounted share. A local volume declared under `mounts` is a mountpoint too,
  and `mountpoint` stats a hard NFS path with no timeout — the pre-start hookscript
  wraps every such stat in `timeout -s KILL` for exactly that reason.
- **`changed_when: "'Pouring' in stdout"`** for `brew install`. `Pouring` is the
  bottle path only; a from-source build of a dependency-free formula prints neither
  `Pouring` nor `Installing`, and an upgrade prints `Upgrading`.
- **Trusting `brew install` to be a no-op for installed formulae.** Its documented
  default is to UPGRADE an installed-but-outdated formula (and its dependents), so
  an operator's `brew update` would arm the next routine deploy to move the encoder
  versions the workbench's ledger records.

## Solution

All in `roles/linuxbrew` and `ansible/inventory/group_vars/workbench_hosts.yml` (#258):

1. **Pin the interpreter** for the whole group:
   `ansible_python_interpreter: /usr/bin/python3`. A literal path is right here —
   every workbench is a Linux CT; CLAUDE.md's "never a literal path" rule is about
   `ansible_connection: local` plays on the two operator platforms.
2. **PATH order** written whole into `/etc/environment`, prefix AFTER the system
   directories: `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:<prefix>/bin:…`.
   With the prefix first, the formulae's dependency tree (util-linux, curl,
   openssl, python@3) shadowed 135 system binaries — `mount`, `findmnt`, `curl`,
   `python3` — and the interpreter move above was one symptom of that. "A brew
   tool wins" is guaranteed by the purge (5) instead, which makes the verify
   step (7) a real detector of a reinstalled apt copy. The root wrapper `/usr/local/bin/brew` (templated) does `cd /` then
   `runuser -u linuxbrew -- <prefix>/bin/brew "$@"`, because brew refuses a cwd the
   brew user cannot read (root's `/root` is 0700) — the install task sets
   `chdir: "{{ linuxbrew_prefix }}"` for the same reason.
3. **Prefix skeleton** `bin`, `Cellar`, `Caskroom` (casks are macOS-only, but
   `brew list --versions` stats the directory and exits 1 without it), and
   `/home/linuxbrew` at 0755 — Ubuntu's 0750 default makes every brew tool
   "command not found" for any non-root user, invisible to a root-only test.
4. **Deploy never moves versions**: `HOMEBREW_NO_INSTALL_UPGRADE=1` and
   `HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1` in the role's install task ONLY
   (`linuxbrew_install_env`); `profile.d` and the wrapper carry just
   `NO_ANALYTICS`/`NO_AUTO_UPDATE`/`NO_ENV_HINTS`, because the dependents-check
   var would also make the operator's own `brew upgrade` skip outdated dependents.
   `changed_when` comes from a `brew list --formula --versions` snapshot before vs
   after.
5. **Bounded purge** of the apt packages brew replaces: `apt-get -s -y purge <list>`
   first, assert the `Purg` set is a SUBSET of the declared list (a declared package
   already gone removes nothing), no `autoremove` (nightly
   unattended-upgrades already removes unused dependencies on guests). Measured to
   fire: `-e linuxbrew_replaces_apt_packages=[python3-mutagen]` → "apt would also
   remove beets, python3-mediafile".
6. **NFS gate** for share directories (`ansible/playbooks/configure-guests.yml` post_tasks):
   `timeout -s KILL 30 /usr/bin/findmnt -n -o FSTYPE,SOURCE --target <parent>`
   (absolute: brew's util-linux ships a `findmnt` too) and assert the fstype is
   `nfs`/`nfs4`; the refusal names which of hung / missing parent / local
   filesystem it saw. `findmnt --target` walks up only for a path that exists, so
   a nested entry needs its parent declared first. Measured to fire on a local zfs
   mount and on a missing parent, `changed=0`.
7. **Verify from the session PATH**, gated on a `stat` of `<prefix>/bin/brew` rather
   than on check mode, so drift shows in a dry-run on a converged host: every
   binary in `linuxbrew_verify_commands` must resolve under the prefix, and a
   verify list shorter than the package list fails the role.

## Why This Works

Every trap here is a PATH or ownership consequence that a root-only, green-recap
test cannot see: root bypasses the traverse check (0750 home), Ansible skips sudo
for `become_user == remote_user` (so the pam_env PATH reaches tasks at all — a
non-root `ansible_user` would get sudo's `secure_path`, which omits the prefix,
and go red), and interpreter discovery is cached per run (so the move shows only on
the NEXT run). The fixes therefore pin what discovery would otherwise resolve, make
the record command's exit status part of the deploy's own assert, and bound the two
destructive steps (purge, mkdir-on-share) by asking the authoritative tool — apt's
simulation, `findmnt`'s fstype — instead of re-implementing its logic.

## Prevention

- After ANY change that reorders PATH on a managed host, run `ansible <host> -m
  ping` and read `discovered_interpreter_python` — on the run after the change, not
  the one that made it. And count what a prefix shadows before putting it ahead of
  `/usr/bin` (`comm -12 <(ls <prefix>/bin) <(ls /usr/bin)`): a toolchain's
  dependency tree is not just the tools you asked for.
- A role that names its own evidence command ("`brew list --formula --versions` is
  the record") asserts that command's rc in the same role.
- Grade guards live-red: the purge bound and the NFS gate each have a one-line
  `-e` falsification in the PR body; run them under `--check` so a guard that fails
  open mutates nothing.

## Related Issues

- #254 / #256 — the workbench pattern and the single-bind rule this builds on.
- [ansible-file-mode-on-nfs-mountpoint-chmods-the-dataset-root.md](ansible-file-mode-on-nfs-mountpoint-chmods-the-dataset-root.md) — the other way an Ansible `file:` task reaches NAS data through a bind.
- [prove-a-guard-whose-failing-state-cannot-exist-yet.md](../conventions/prove-a-guard-whose-failing-state-cannot-exist-yet.md) — `apt-get -s` as the dependency oracle (#247).
- [lxc-systemd-259-needs-nesting-credentials-243.md](lxc-systemd-259-needs-nesting-credentials-243.md) — the same CT's boot trap.

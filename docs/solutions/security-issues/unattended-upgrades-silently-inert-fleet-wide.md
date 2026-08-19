---
title: "Unattended-upgrades was never installed, then matched nothing: two stacked silent-green failures"
date: 2026-08-15
category: security-issues
module: common
problem_type: security_issue
component: tooling
symptoms:
  - "apt-daily-upgrade.timer enabled and active on all 4 hosts while /usr/bin/unattended-upgrade does not exist"
  - "/etc/apt/apt.conf.d/50unattended-upgrades deployed and correct-looking, /var/log/unattended-upgrades never created"
  - "Every Ansible run reports ok/changed with failed=0 — zero unattended security patching fleet-wide"
  - "Origins-Pattern using a bare distro_codename matches no Debian security archive (its Codename is release-security)"
  - "rsync silently dropped from the install list; synchronize-based service deploys work only by LXC-template luck"
root_cause: config_error
resolution_type: config_change
severity: critical
related_components:
  - ansible-inventory
  - unattended-upgrades
  - apt
  - systemd-timers
  - synchronize
tags:
  - ansible
  - variable-precedence
  - unattended-upgrades
  - security-updates
  - group-vars
  - origins-pattern
  - silent-failure
---

# Unattended-upgrades was never installed, then matched nothing: two stacked silent-green failures

## Problem

Unattended (nightly) security updates were dead on every host in the fleet, and had been for the
entire life of the `common` role. Two independent defects sat on top of each other, and **fixing
only the first one would have left the system just as unpatched, and just as green.**

(Patching was not literally zero: the same role ended with an unconditional
`apt: upgrade: safe`, so packages moved whenever someone ran the playbook. That task is now gated
behind `apt_apply_pending_upgrades` (default false) — #97 — and it was never automatic patching;
between runs, nothing happened.)

1. Inventory `group_vars` replaced the role's package list, so `unattended-upgrades` was never
   installed — while the role kept deploying its config files.
2. Even once installed, the deployed `Origins-Pattern` matched no real archive on any Debian
   host, so a correctly-installed, correctly-timed nightly run would have upgraded nothing.

This was never a regression. `git log --follow` shows the role default and its shadowing
`group_vars/all` duplicate were introduced in the **same commit** — `d6d82a8` (#1, 2026-04-28),
the initial Ansible migration. The mechanism was inert from the first day it existed, which is
why no change ever appeared to break it.

## Symptoms

The signature is a system that reports healthy while the control is entirely absent:

- `/etc/apt/apt.conf.d/20auto-upgrades` and `50unattended-upgrades` present on all four hosts.
- `systemctl is-enabled apt-daily-upgrade.timer` → `enabled`; `is-active` → `active`.
- `/usr/bin/unattended-upgrade` **does not exist**. The timer fires nightly and executes nothing.
- `/var/log/unattended-upgrades` was never created — no log, because nothing ever ran.
- `dpkg-query -W unattended-upgrades` → `no packages found matching unattended-upgrades`.
- Every `ansible-playbook` run: `failed=0`. Nothing anywhere reports a problem.

## What Didn't Work

- **"Just delete the `common_packages` override from `group_vars`."** This is the obvious
  one-line fix and it is genuinely necessary — but it is *not sufficient*. It restores the
  package, the binary, and the nightly run, and then the nightly run reports "no packages can be
  upgraded unattended" **forever**, because of layer 2. The observable end state after this fix
  looks better and is exactly as unpatched. A dry-run confirming `unattended-upgrades` is now
  installed feels like proof, and proves only that a package was installed.

- **Putting the protected package list in the role's `defaults/`.** The first attempt introduced
  `common_role_required_packages` in `ansible/roles/common/defaults/main.yml` and asserted over
  it.
  `defaults/` is the **lowest** precedence source in Ansible — *below* inventory `group_vars`,
  the very mechanism that caused the bug. A one-line `group_vars` entry would have removed the
  packages from the install list **and** from the assert's own loop simultaneously: the guard
  passes, the fleet goes unpatched, nothing reports. A guard whose control input is overridable
  by the thing it guards against is not a guard.

- **Trusting the config file and the timer as evidence.** Both were present and correct-looking
  for the whole outage. Neither says anything about whether the mechanism runs.

- **Trusting `--check` to surface it.** The guard was initially gated `when: not
  ansible_check_mode` and so said nothing at all during the dry-run that
  [CLAUDE.md](../../../CLAUDE.md) rule 5 makes the standard first pass.

## Solution

**Layer 1 — put role dependencies out of inventory's reach.** Role `vars/` outranks inventory
`group_vars` and `host_vars`, so a new `ansible/roles/common/vars/main.yml` holds the packages
the role itself depends on:

```yaml
# ansible/roles/common/vars/main.yml (new)
common_role_required_packages:
  - rsync                # ansible.posix.synchronize — every service deploy
  - unattended-upgrades  # provides /usr/bin/unattended-upgrade
  - apt-listchanges
  - python3-yaml
```

`common_packages` stays in `defaults/` for operator extras, and the install task unions the two:

```yaml
# ansible/roles/common/tasks/main.yml
- name: Install common packages
  ansible.builtin.apt:
    name: "{{ (common_role_required_packages + common_packages) | unique }}"
    state: present
```

The duplicated `common_packages` block was deleted from
`ansible/inventory/group_vars/all/vars.yml`.

**Layer 2 — match the origin that actually exists.** Verified against `/var/lib/apt/lists` on
the live fleet:

| Host | Release | Origin | Label | Codename |
| ---- | ------- | ------ | ----- | -------- |
| eq12, n5pro | trixie | Debian | Debian-Security | `trixie-security` |
| eq12_docker | bookworm | Debian | Debian-Security | `bookworm-security` |
| n5pro_docker | noble | Ubuntu | Ubuntu | `noble` |

```
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Ubuntu,codename=${distro_codename},label=Ubuntu";
    "origin=Ubuntu,codename=${distro_codename}-security,label=Ubuntu";
};
```

**Layer 3 — assert the artifact, loudly, in both modes.** Compute the missing set once, hard-fail
on a live run, and *report by name* under `--check` instead of staying silent:

```yaml
- name: Gather installed package facts
  ansible.builtin.package_facts:
    manager: apt
  check_mode: false

- name: Determine which role-required packages are missing
  ansible.builtin.set_fact:
    common_missing_required: "{{ common_role_required_packages | difference(ansible_facts.packages.keys() | list) }}"

- name: Assert role-required packages are installed
  ansible.builtin.assert:
    that:
      - common_role_required_packages | length > 0
      - common_missing_required | length == 0
  when: not ansible_check_mode

- name: Report missing role-required packages (check mode)
  ansible.builtin.debug:
    msg: "CHECK MODE — missing role-required package(s): {{ common_missing_required | join(', ') }}. …"
  when:
    - ansible_check_mode
    - common_missing_required | length > 0
```

The paired debug task is what keeps the dry-run honest: the *failure* is gated on check mode,
the *observation* is not.

The non-empty check matters because `-e common_role_required_packages=[]` outranks even role
vars and would otherwise make the emptiness check vacuously true.

A separate `stat` + `assert` covers `/usr/bin/unattended-upgrade` — the artifact the timer
actually executes, and immune to a half-configured (`iU`/`iF`) package that still registers as
installed. Finally the role now owns `apt-daily.timer` and `apt-daily-upgrade.timer` via
`ansible.builtin.systemd_service` rather than trusting the distro preset.

All three legs — binary, config, timer — are now **enforced** rather than assumed. Note the
asymmetry: only the binary is genuinely *asserted*. The config is copied and the timer is
enforced, and nothing yet verifies that the `Origins-Pattern` resolves a real origin on the live
host — the check that would have caught layer 2 automatically. `unattended-upgrade --dry-run
--debug` prints the origins it resolved and is the obvious candidate, but this fix does not
automate it.

**Since closed:** the unconditional `apt: upgrade: safe` that ended this role is now gated behind
`apt_apply_pending_upgrades` (default false), so it runs only as a deliberate maintenance action
(#97). **Still open in this role:** the complete absence of failure signalling for
unattended-upgrades (#99) — a failed nightly run is still invisible. This is a restored control,
not a finished one.

## Why This Works

**Ansible variable precedence.** The ordering that matters here is:

```
role defaults/  <  inventory group_vars / host_vars  <  role vars/  <  -e extra-vars
```

Role `defaults/` sits near the bottom of Ansible's precedence list and inventory sources sit
well above it — which is why the `group_vars` copy won. Role `vars/main.yml` sits above every
inventory source, which is why moving the list there puts it out of inventory's reach. Only `-e`
extra-vars still overrides it, and that is a deliberate operator act rather than an accident of
file layout. Critically, Ansible **replaces** list variables wholesale — it never merges them —
so a `group_vars` list that omits an item deletes that item, silently, with no diff and no
warning. That replacement behaviour *is* this bug.

Both halves were verified empirically rather than taken from documentation. A throwaway role
with `demo_required` in `vars/` and `demo_extras` in `defaults/`, both overridden in
`group_vars/all`, resolved to `required=['from_role_vars'] extras=['OVERRIDDEN_BY_INVENTORY']`.
Then on the real role, planting `common_role_required_packages: [zzz-bogus-override-probe]` in
`group_vars/all` and re-running the dry-run showed the probe ignored and `unattended-upgrades`
still resolving for install.

**Debian's security archive codename.** Since bullseye, Debian's security archive publishes
`Codename: <release>-security` (e.g. `trixie-security`) rather than the bare release codename.
`Origins-Pattern` is matched against that Release metadata verbatim, so
`codename=${distro_codename}` cannot match it. Debian's own shipped `50unattended-upgrades`
carries both spellings for exactly this reason. Ubuntu went the other way: it keeps the bare
codename and labels its security pocket `Ubuntu`, not `Ubuntu-Security` — which is why the
single Ubuntu host in this fleet was the one host where the old pattern *did* match, and why
spot-checking one host would have missed the bug.

## Prevention

- **Never duplicate a role's dependency list into inventory.** Inventory `group_vars` outrank
  role `defaults/`, and lists are replaced, not merged. Packages a role's own tasks depend on
  belong in `roles/<role>/vars/main.yml`, where inventory cannot reach them; `defaults/` is for
  things operators are *meant* to override.
- **Test a guard by attacking it.** Plant a bogus override at the precedence level you are
  worried about and confirm it is ignored. If overriding one variable disables both the behaviour
  and its check, the check is decorative.
- **Assert the artifact, not the proxy.** A config file existing and a timer being `active`
  proved nothing here. The absence of `/usr/bin/unattended-upgrade` and of
  `/var/log/unattended-upgrades` was the only honest signal. Prefer asserting the thing that
  does the work.
- **`state: present` reporting `ok` is not proof of a working binary.** A half-configured
  (`iU`/`iF`) package still registers as installed in `package_facts`.
- **Verify origin/matching patterns against live archive metadata**, never from memory:
  `grep -h -E '^(Origin|Label|Codename|Suite):' /var/lib/apt/lists/*security*Release`. After the
  fix, `unattended-upgrade --dry-run --debug` prints the allowed origins it actually resolved.
- **A guard that is silent in `--check` is untested in the mode you run first.** Gate the
  *failure*, not the *observation* — gather facts with `check_mode: false` and report findings by
  name in check mode even where hard-failing would be wrong.
- **Spot-checking one host hides heterogeneity.** Three Debian hosts across two releases, plus
  one Ubuntu host, did not behave alike; the only host where the old pattern matched was the
  Ubuntu one — a plausible host to have checked, had anyone checked just one.
- **A control that never worked has no "before" to compare against.** Both halves of this bug
  shipped in the same initial commit, so there was no regression, no failing run, and no drift to
  detect — only the absence of an effect nobody had asserted. New security controls need their
  first verification at the moment they are introduced, because after that there is nothing to
  notice.

## Related Issues

- #77 — this fix (`common` role; fleet-wide unattended security updates).
- #97 — the same role's unconditional `Apply pending security upgrades` task mass-upgraded the
  fleet on every run; discovered while deploying this fix. Since fixed: the task is gated behind
  `apt_apply_pending_upgrades` (default false) and renamed to say what it actually does.
- #99 — unattended-upgrades still has no failure signalling (no Mail/MTA, logs not shipped), so a
  failed nightly run remains invisible.
- #100 — `proxmox_guests` always-changed tasks break the idempotency signal.
- #101 — reboot window needed for the hypervisors after this deployment's upgrade wave.
- [vector-057-silent-log-pipeline-failure](../integration-issues/vector-057-silent-log-pipeline-failure.md)
  — the same failure *class*: config that looks deployed while the mechanism is inert.
- [watchtower-label-enable-scan-scope](../integration-issues/watchtower-label-enable-scan-scope.md)
  — another control silently out of scope while reporting healthy.
- [ansible-change-loop-pitfalls](../conventions/ansible-change-loop-pitfalls.md) — check-mode
  safety and idempotency gates.

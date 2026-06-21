---
title: "dhclient AppArmor AF_UNIX socket denials flood the Proxmox console (PVE 9 / AppArmor 4.1)"
date: 2026-06-20
category: integration-issues
module: proxmox_host
problem_type: integration_issue
component: tooling
symptoms:
  - "apparmor=\"DENIED\" operation=\"create\" class=\"net\" info=\"failed protocol match\" profile=\"/{,usr/}sbin/dhclient\" comm=\"dhclient\" family=\"unix\" sock_type=\"dgram\""
  - "KVM console and journalctl -k flooded with ~78 kernel audit denials per hour, in bursts"
  - "dhclient unix dgram socket create denied with error=-13 even after adding an allow rule to the profile"
root_cause: config_error
resolution_type: config_change
severity: low
related_components:
  - apparmor
  - dhclient
  - proxmox
  - debian
tags:
  - apparmor
  - dhclient
  - proxmox
  - af-unix
  - abi
  - networking
---

# dhclient AppArmor AF_UNIX socket denials flood the Proxmox console (PVE 9 / AppArmor 4.1)

## Problem

After the Proxmox VE 9 upgrade (AppArmor 4.1, kernel 7.0.x-pve), the n5pro KVM
console floods with kernel audit denials — ~78/hour, in bursts — every time
`dhclient` touches a unix datagram socket. DHCP still works; the denials are pure
console/`journalctl -k` noise, but they drown out real kernel events.

The root cause is an **AppArmor ABI mismatch**, not a missing rule. PVE 9 ships
AppArmor 4.1, whose default policy ABI enforces fine-grained `AF_UNIX` socket
mediation that the shipped `/etc/apparmor.d/usr.sbin.dhclient` profile predates —
it declares only `network inet/inet6/packet/raw` rules and nothing covering
`AF_UNIX`. When `dhclient` opens a unix dgram socket (glibc's syslog/NSS path),
the af_unix-capable kernel default-denies it.

Only hosts whose interface actually runs `dhclient` are affected:

| Host  | `vmbr0` config | Runs dhclient? | Affected? |
| ----- | -------------- | -------------- | --------- |
| n5pro | `inet dhcp`    | yes            | yes       |
| eq12  | `inet static`  | no             | no        |

Diagnostic breadcrumbs that confirm the ABI gap:

```bash
# Kernel advertises af_unix network mediation...
cat /sys/kernel/security/apparmor/features/network_v9/af_unix   # -> yes
# ...there is NO fine-grained 'unix' feature dir, and the parser is pinned to a
# static (network_v8) feature file that is blind to it:
grep policy-features /etc/apparmor/parser.conf
# -> policy-features=/usr/share/apparmor-features/features
```

> The denial flood was initially suspected in a June 4 hard freeze but was
> **exonerated**: the crash boot showed the same steady cadence with no spike and
> no panic/OOM/MCE. The freeze is tracked separately (issue #43).

## Symptoms

- KVM console and `journalctl -k` flooded ~78 denials/hour, in bursts.
- Audit lines like:

  ```
  apparmor="DENIED" operation="create" class="net" info="failed protocol match" \
  error=-13 profile="/{,usr/}sbin/dhclient" comm="dhclient" family="unix" \
  sock_type="dgram" protocol=0 requested="create" denied="create" addr=none
  ```

- Only on the DHCP-addressed host (n5pro); the static-addressed host (eq12) was
  clean (no dhclient process).
- Networking itself unaffected — purely log/console noise.

## What Didn't Work

Each was disproven by a live test under the profile (see Prevention for the test
method). Documented so nobody re-walks the path.

1. **Fine-grained `unix (...) type=dgram,` rule in the local override.** Added to
   `/etc/apparmor.d/local/usr.sbin.dhclient` + `apparmor_parser -r`. Still denied —
   this kernel mediates `AF_UNIX` via the **network class**, not the fine-grained
   `unix` class (there is no `features/unix` dir, only `features/network/af_unix`).

2. **`network unix dgram,` alone in the local override.** The rule compiled into
   the profile (confirmed with `apparmor_parser -p`) and test-compiled clean
   (`apparmor_parser -Q`, exit 0), yet was *still denied* — because the profile is
   compiled against an ABI blind to af_unix. The grant is correct but inert without
   the ABI fix.

3. **`abi <abi/3.0>,` placed in the local override.** Some Proxmox-forum posts
   suggest adding the abi line to `/etc/apparmor.d/local/...`. Still denied — an
   `abi` declaration is **inert when it appears inside the profile block or via a
   late `#include`** (the local override is `#include`d at the end, *inside* the
   braces). This is exactly why forum reports of "the abi fix" were mixed.

## Solution

Two parts, both encoded in the `proxmox_host` Ansible role, gated on the base
profile existing, and reloaded by a single `apparmor_parser -r` handler.

**1. Pin the profile to the 3.0 ABI — in the PREAMBLE (file top, before the
profile block).** The 3.0 ABI uses coarse network-class `AF_UNIX` mediation, which
the grant in step 2 can satisfy. Use `insertbefore: BOF` so placement is
guaranteed regardless of any anchor line.

```yaml
- name: Pin dhclient AppArmor profile to the 3.0 ABI (preamble)
  ansible.builtin.lineinfile:
    path: /etc/apparmor.d/usr.sbin.dhclient
    insertbefore: BOF          # guarantees preamble placement; no anchor dependency
    line: "abi <abi/3.0>,"
  when: dhclient_apparmor_profile.stat.exists
  notify: reload-apparmor-dhclient
```

**2. Grant the unix dgram socket in the local override.**

```yaml
- name: Allow dhclient unix dgram socket (AppArmor local override)
  ansible.builtin.copy:
    content: |
      network unix dgram,
    dest: /etc/apparmor.d/local/usr.sbin.dhclient
    mode: "0644"
  when: dhclient_apparmor_profile.stat.exists
  notify: reload-apparmor-dhclient
```

**3. Assert the pin actually precedes the profile block** so a future profile
reformat (or a misplaced edit) can't silently make the fix inert:

```yaml
- name: Read back the dhclient profile to verify the abi pin placement
  ansible.builtin.slurp:
    src: /etc/apparmor.d/usr.sbin.dhclient
  register: dhclient_profile_content
  check_mode: false

- name: Assert the abi pin precedes the profile block (else the fix is inert)
  vars:
    profile_text: "{{ dhclient_profile_content.content | b64decode }}"
  ansible.builtin.assert:
    that:
      - "profile_text.find('abi <abi/3.0>,') != -1"
      - "profile_text.find('abi <abi/3.0>,') < profile_text.find('/{,usr/}sbin/dhclient ')"
    fail_msg: "'abi <abi/3.0>,' is missing or not in the preamble — the fix would be inert."
```

The handler:

```yaml
- name: Reload AppArmor dhclient profile
  ansible.builtin.command: apparmor_parser -r /etc/apparmor.d/usr.sbin.dhclient
  changed_when: true
  listen: reload-apparmor-dhclient
```

Net effect on the profile file:

```diff
 # vim:syntax=apparmor
+abi <abi/3.0>,
 #include <tunables/global>

 /{,usr/}sbin/dhclient flags=(attach_disconnected) {
   #include <abstractions/base>
   ...
   #include <local/usr.sbin.dhclient>   # <- "network unix dgram," lands here
 }
```

## Why This Works

The denial is a **compilation-ABI** problem, not a missing-rule problem. The
af_unix-capable kernel mediates unix sockets through the network class, but the
profile — as compiled against PVE 9's default feature set — has no representation
for af_unix in that class, so any unix socket is default-denied no matter what
rule you add.

Pinning `abi <abi/3.0>` in the preamble compiles the *entire* profile against the
3.0 feature ABI, where `AF_UNIX` is mediated coarsely by the network class. That
makes the kernel honor a coarse `network unix dgram,` grant. **Both halves are
required**: the ABI pin makes the grant meaningful, and the grant makes the socket
allowed. The abi line only takes effect from the preamble because abi resolution
happens at profile-load time for the whole profile — declared inside the block (or
via a late include) it is parsed but never applied.

## Prevention

- **Verify deterministically — run a process *under* the profile, don't wait on
  the daemon:**

  ```bash
  aa-exec -p "/{,usr/}sbin/dhclient" -- \
    python3 -c "import socket; socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); print('OK')"
  # EACCES when broken, "OK" when fixed
  ```

- **Don't trust short denial-free windows.** The denials are bursty (clusters,
  then multi-minute gaps); a 6-minute quiet window produced a false "fixed" once.
  Confirm over **8+ minutes** *and* with the `aa-exec` test. Post-fix result here:
  0 denials over 8 min vs 16 in the equivalent pre-fix window.

- **Grep pitfall — `DENIED` appears *before* `dhclient` in the audit line,** so
  `grep "dhclient.*DENIED"` returns a **false zero**. Use:

  ```bash
  journalctl -k | grep DENIED | grep dhclient
  # or:  journalctl -k | grep 'comm="dhclient"' | grep DENIED
  ```

- **The preamble pin edits a dpkg-owned file** (`/etc/apparmor.d/usr.sbin.dhclient`);
  an `apparmor-profiles` package upgrade can revert it (the local override
  survives). Re-run the `proxmox_host` role to restore. The `assert` catches a
  silently-misplaced pin.

- **When an AppArmor profile breaks right after a major distro/AppArmor upgrade,
  suspect an ABI mismatch first.** Pin the profile's ABI in the **preamble**, not
  the local include.

## Related

- [Docker Compose services fail to start in privileged LXC due to AppArmor docker-default profile](./docker-apparmor-privileged-lxc.md) — sibling AppArmor-on-Proxmox issue, **different mechanism**: that fix *disables* AppArmor for containers (`security_opt: apparmor:unconfined`); this one keeps it enforcing and fixes the host profile's ABI.
- [Ansible change-loop pitfalls](../conventions/ansible-change-loop-pitfalls.md) — the fix uses `lineinfile insertbefore: BOF` and must stay idempotent across `proxmox_host` re-runs.
- Upstream: [apparmor#561](https://gitlab.com/apparmor/apparmor/-/issues/561). Originating fix: issue #42. Crash follow-up: issue #43.

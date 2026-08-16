---
title: "Create-time-only guest fields are rebuild declarations — write them in allocation syntax, not live syntax"
date: 2026-08-16
category: integration-issues
module: proxmox_guests
problem_type: integration_issue
component: tooling
symptoms:
  - "CT 101's `mp0` named an already-allocated volume (`local-zfs:subvol-101-disk-1,size=110G,mp=/data,backup=1`) — a form Proxmox never allocates"
  - "Nothing ever reports it: provisioning is pinned create-only, so for an existing guest the module exits `changed=false` without evaluating `mounts` at all"
  - "`--check --diff` cannot exercise the field on a live fleet — the only run that consumes it is a from-scratch rebuild, i.e. a disaster recovery"
  - "A rebuilt host would configure `/data` to mount a ZFS dataset that does not exist, instead of allocating one"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - proxmox
  - lxc
  - ansible
  - host_vars
tags:
  - ansible
  - proxmox
  - lxc
  - disaster-recovery
  - rebuild-fidelity
  - create-time-only
  - silent-failure
---

# Create-time-only guest fields are rebuild declarations — write them in allocation syntax, not live syntax

## Problem

Once [[community-proxmox-update-default-blind-config-put]] (#86) pinned both Proxmox
provisioning modules to `update: false`, a whole class of guest fields stopped being
"configuration that converges" and became something else: a **declaration consumed only by a
from-scratch rebuild**. `mounts`, `scsi`, `bios`, `machine`, `efidisk0`, `usb`, `agent`,
`balloon`, `cpu`, `localtime`, `tablet`, `hostpci`, `ide`, `scsihw`, `tags`, `description` and
`name` are among the fields passed at create and never reconciled afterwards; the role's own
CREATE-TIME ONLY comment blocks in `ansible/roles/proxmox_guests/tasks/main.yml` carry the
authoritative per-guest-type lists.

CT 101's mount point was written in the syntax that describes the volume that exists today:

```yaml
mp0: "local-zfs:subvol-101-disk-1,size=110G,mp=/data,backup=1"
```

That string is a perfect match for `pct config 101`. It is also the one form Proxmox will
never allocate — so the line whose only job was to tell a rebuild what to create could not
create anything.

## Symptoms

- `ansible/inventory/host_vars/eq12/vars.yml` declared `mp0` as a named subvol; CT 201 on
  n5pro already used the allocation form (`local-zfs:200,mp=/data`), and nothing flagged the
  inconsistency.
- No run reports it. Provisioning is create-only, so for an existing guest the module returns
  `changed=false` without ever looking at `mounts`.
- `--check --diff` against the live fleet is structurally blind here: the only execution that
  reads this value is a rebuild.
- The failure would therefore land during disaster recovery — the one moment the automation
  is load-bearing and the operator has the least slack.

## What Didn't Work

- **Comparing the declaration against the live host.** It matched exactly, which is precisely
  what made it look correct. The rule this repo had already written down —
  *"declare live resources in the syntax they actually exist in"* from the #86 doc — actively
  endorsed the broken form. That rule was written for the reconcile-task half of the config
  surface, where a declaration is compared against live state; it is the wrong instruction for
  the create-only half, where the declaration is a build order.
- **Trusting a green `task infra:hosts` / `--check --diff`.** For create-time-only fields a
  green run carries no information whatsoever: the module short-circuits before the field is
  read. Absence of drift is not evidence of a working declaration when nothing consumes it.

## Solution

Write the field in the syntax a fresh create needs, and demote the live value to a comment:

```yaml
mounts:
  # Allocation form (like CT 201): `mounts` is consumed at CREATE only
  # (provisioning is pinned update:false, #86), so this line's only job is
  # to tell a from-scratch rebuild what to allocate — naming a subvol here
  # does not allocate anything on a rebuilt host (#87). Live volume:
  # local-zfs:subvol-101-disk-1, 110G, backup=1 (pct config 101, 2026-08-16).
  # DR: a rebuild allocates a FRESH EMPTY /data — restore it from the vzdump
  # backup (backup=1 includes mp0) before deploying services. Nothing
  # enforces that ordering yet — tracked in #127.
  mp0: "local-zfs:110,mp=/data,backup=1"
```

Live state is still recorded — as a dated comment, where it informs the operator without
being mistaken for a build instruction.

## Why This Works

Proxmox decides whether to allocate by pattern-matching the volume string. From
`/usr/share/perl5/PVE/LXC.pm:63` (pve-manager 9.2.10):

```perl
our $NEW_DISK_RE = qr/^([^:\s]+):(\d+(\.\d+)?)$/;
```

Only `<storage>:<size>` matches. `create_disks` then branches on it
(`/usr/share/perl5/PVE/LXC.pm:2669`):

```perl
if ($storage && ($volid =~ $NEW_DISK_RE)) {
    # ... alloc_disk(...) -> PVE::Storage::vdisk_alloc(...)
} else {
    # use specified/existing volid/dir/device
    $conf->{$ms} = PVE::LXC::Config->print_ct_mountpoint($mountpoint, $ms eq 'rootfs');
}
```

So `local-zfs:110` allocates a new 110 GiB dataset, while
`local-zfs:subvol-101-disk-1,size=110G` takes the `else` branch — *use existing* — and writes
the mountpoint into the config **without allocating anything**.

**A precision worth keeping — where this actually fails.** It is not a clean "bad parameter"
rejection. The create-path storage checks in `/usr/share/perl5/PVE/API2/LXC.pm` verify that the
*storage* is enabled and supports container directories; nothing validates that a named
*volume* exists, and `parse_volume_id` is a pure string parser. The failure instead lands
**inside the same `pct create` invocation, one step later**: `create_vm` calls
`PVE::LXC::mount_all()` immediately after `create_disks()` and before the template is
unpacked, `mount_all` activates every mountpoint including the one that was never allocated,
and the ZFS plugin dies querying a dataset that does not exist.

So the old form was late but *loud* — it aborted the create with a raw ZFS-level error rather
than a legible Proxmox-level one. That distinction matters in both directions: it is not a
silent success (the container does not come up half-built), and it is not a helpful early
refusal either. Diagnosing a `zfs error` mid-rebuild, during a recovery, is exactly the wrong
time to be reverse-engineering which inventory field asked for a volume that was never made.

## Prevention

- **Ask what consumes a field before deciding how to write it.** A field a reconcile task
  compares against live state must match live. A create-time-only field is a build order and
  must be written in the syntax the create path accepts. The two rules are opposites, and the
  field list decides which applies — see the CREATE-TIME ONLY comment block in
  `ansible/roles/proxmox_guests/tasks/main.yml`.
- **Record live state in a comment, never in the value**, and date it (`pct config 101,
  2026-08-16`). This keeps the operator's information without letting it masquerade as a
  declaration.
- **Treat "no drift reported" as no information for create-only fields.** Verification has to
  come from the tool's create path — read which syntax the allocator actually accepts — rather
  than from a dry-run that never reaches the field.
- **Know what the fix traded away.** The old named form would at least have broken loudly on a
  rebuild. The allocation form succeeds and hands the deploy a *fresh empty* `/data`, so a
  service stack can now deploy onto an empty volume and look healthy. Restoring the vzdump
  backup before `deploy-services` is currently operator discipline, not an enforced gate —
  that gap is #127, and it is the direct cost of this fix.

## Related Issues

- #87 — this fix: fresh-rebuild reproducibility (CT 101 mount form, plus the SSH-hardening
  lockout guard, the missing `wait_for_connection`, and the wrong n5pro default bridge).
- #86 — [[community-proxmox-update-default-blind-config-put]]. This doc **refines** its
  Prevention rule *"declare live resources in the syntax they actually exist in"*: that rule
  predates the `update: false` pin and is correct only for reconciled fields. For create-only
  fields the instruction inverts. The #86 doc already forward-references #87 as the place this
  refinement belongs.
- #127 — fresh rebuild deploys services onto a silently empty `/data`; no enforced restore
  gate between guest creation and `deploy-services`. Open, and created by this fix.
- [[lxc-features-nfs-invalid-key-silent-green]] — sibling defect in the same role from the same
  PR: a value the tool rejected every run while the play stayed green.
- [../runtime-errors/proxmox-boot-order-inversion-breaks-nfs-volume-mount.md](../runtime-errors/proxmox-boot-order-inversion-breaks-nfs-volume-mount.md)
  — the predecessor in this family: create-only fields (`startup`/`onboot`) silently never
  reaching an existing guest.
- [../conventions/ansible-change-loop-pitfalls.md](../conventions/ansible-change-loop-pitfalls.md)
  — the general doctrine this is an instance of: a green run is not evidence a mechanism ran.

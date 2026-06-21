---
title: "Proxmox boot-order inversion breaks an NFS-backed Docker volume mount"
date: 2026-06-20
category: runtime-errors
module: proxmox_guests
problem_type: runtime_error
component: tooling
root_cause: config_error
resolution_type: config_change
severity: high
symptoms:
  - "docker compose up fails: failed to populate volume ... addr=10.99.99.2 ... connection refused"
  - "lms (Lyrion) container stuck Exited 137 for weeks; restart:unless-stopped never recovers it"
  - "qm config 200 shows onboot:1 but NO startup order, despite host_vars setting startup: order=1"
  - "TrueNAS (NFS provider) boots after the docker LXC (consumer) on host reboot"
related_components:
  - truenas
  - nfs
  - lms
  - proxmox
  - proxmox_guests
tags:
  - proxmox
  - boot-order
  - nfs
  - truenas
  - ansible
  - docker-compose
  - systemd
---

# Proxmox boot-order inversion breaks an NFS-backed Docker volume mount

## Problem

The lms (Lyrion) Docker stack on `n5pro-docker` (CT 201) repeatedly failed to
mount its NFS-backed `/music` from the TrueNAS VM, leaving the container
`Exited (137)` for ~4 weeks. The recurring root cause was a **Proxmox boot-order
inversion**: TrueNAS VM 200 had silently lost its `startup` order, so the NFS
*consumer* (the LXC) booted before the *provider* (TrueNAS), and the docker
`local` NFS volume failed at container-create time.

> This is a **different, durable** cause from the one-off
> [[truenas-nfs-service-down-after-crash]] (NFS service auto-start left off after
> a crash). Same symptom, different fix — see Related Issues for the differential.

## Symptoms

- `docker compose up` for lms fails at volume creation:
  ```
  failed to populate volume lyrion_lms_music: mount :/mnt/vault/media/music/lossless
  addr=10.99.99.2,nfsvers=4 ... connection refused
  ```
- The container sits `Exited (137)` for weeks; `restart: unless-stopped` never
  recovers it because a `local` NFS volume is mounted only at container-create
  time, not retried.
- `qm config 200` shows `onboot: 1` but **no `startup:` line** — TrueNAS is an
  *unordered* guest.
- Proxmox boots all `startup`-ordered guests **before** unordered ones, so the
  ordered LXC (CT 201, `order=2`) comes up while unordered TrueNAS is still
  booting (PCIe passthrough + ZFS import + NFS start) — NFS isn't serving yet.

## What Didn't Work

- **The post-crash runbook ("restart TrueNAS / re-enable the NFS service").**
  Correct for a one-off NFS-service-down after a TrueNAS crash, but it did not
  address the *recurring* cause — the order drift returns the failure on every
  reboot.
- **Redeploying lms alone.** Re-running the lms deploy once TrueNAS was up fixed
  it *that time* but did nothing to boot ordering, so it broke again next boot.
- **Relying on the LXC's `up=60`.** The wait was on the wrong guest. Proxmox's
  `up=N` delays the *next* guest in the order, not the guest it is set on — so a
  delay on the consumer LXC bought nothing (and with TrueNAS unordered there was
  no ordering relationship to delay anyway).

## Solution

Two layers: fix the boot order at the Proxmox level, and add a self-heal backstop
in the LXC.

**1. Reconcile `startup`/`onboot` on existing guests.**
`community.proxmox.proxmox_kvm` / `proxmox` with `state: present` only *creates* —
it never updates an existing guest, so `startup` edits in vars never reached VM
200. Add explicit `qm set` / `pct set` reconcile tasks (VM shown; the LXC task is
identical with `pct config` / `pct set`):

```yaml
- name: Reconcile startup/onboot on existing VMs
  become: true
  ansible.builtin.shell: |
    set -euo pipefail   # -e so a failed qm set aborts instead of falsely echoing "changed"
    args=""
    desired_startup="{{ item.startup | default('') }}"
    desired_onboot="{{ (item.onboot | default(proxmox_vm_defaults.onboot)) | ternary(1, 0) }}"
    current_startup=$(qm config {{ item.vmid }} | sed -n 's/^startup: //p')
    current_onboot=$(qm config {{ item.vmid }} | sed -n 's/^onboot: //p')
    current_onboot=${current_onboot:-0}
    if [ -n "$desired_startup" ] && [ "$current_startup" != "$desired_startup" ]; then
      args="$args -startup $desired_startup"
    fi
    if [ "$current_onboot" != "$desired_onboot" ]; then
      args="$args -onboot $desired_onboot"
    fi
    if [ -n "$args" ]; then
      qm set {{ item.vmid }} $args
      echo "changed"
    fi
  register: vm_startup_update
  changed_when: vm_startup_update.stdout == "changed"
  loop: "{{ proxmox_vms }}"
```

With corrected vars: TrueNAS `startup: "order=1,up=180"` (the wait lives on the
**provider**), docker LXC `startup: "order=2"` (the `up=60` was removed).

**2. Self-heal oneshot systemd unit in the lms role**
(`lms-nfs-heal.service.j2` + `lms-nfs-heal.sh.j2`, ordered
`After=docker.service network-online.target`). It waits until NFS is actually
*serving the export*, then re-ups the stack. The readiness probe confirms the
export is **published**, not just that nfsd is registered:

```bash
nfs_ready() {
  rpcinfo -T tcp "$NFS_ADDR" nfs >/dev/null 2>&1 \
    && showmount -e "$NFS_ADDR" 2>/dev/null | grep -q "$NFS_PATH"
}
until nfs_ready; do
  [ "$waited" -ge "$MAX_WAIT" ] && { echo "...giving up." >&2; exit 1; }
  sleep "$INTERVAL"; waited=$((waited + INTERVAL))
done
cd "$DEPLOY_DIR"
docker compose -f "$COMPOSE_FILE" --env-file .env up -d
```

Verified live including a full host reboot: TrueNAS boots first, the LXC is held
back, lms comes up healthy unattended.

## Why This Works

- **Create-only module limitation:** `proxmox_kvm` / `proxmox` with
  `state: present` never updates an existing guest, so `startup` vars silently
  drifted. The `qm set` / `pct set` reconcile tasks make "edit vars, re-run"
  actually enforce the value (config-only; no guest restart).
- **Ordered-before-unordered boot rule:** Proxmox starts all `startup`-ordered
  guests before unordered ones. Once TrueNAS regains `order=1` it provably boots
  before the `order=2` consumer.
- **`up=` applies to the next guest:** Proxmox's `up=N` holds the *next* guest in
  order, so the wait belongs on TrueNAS (`order=1,up=180`) to hold back the LXC —
  not on the LXC, where it does nothing.
- **Self-heal backstop:** A docker `local` NFS volume mounts only at create time
  and `restart: unless-stopped` cannot remount a failed volume. The oneshot waits
  until the export is genuinely published (`showmount` confirms it, so a `soft`
  mount can't succeed against a missing export mid-ZFS-import) and re-ups the
  stack — recovering even if 180s is ever too short.

## Prevention

- **Reconcile-on-existing pattern:** Any guest attribute set via a `state: present`
  module (startup, onboot, net, features) drifts on already-created guests. Pair
  each with an explicit `qm set` / `pct set` reconcile task guarded by a
  `current != desired` diff so re-running the playbook enforces it. (The existing
  "Update VM network interfaces on existing VMs" task is the precedent.)
- **Put `up=` on the provider, not the consumer.** Boot delays hold the *next*
  guest in order — encode the wait on the dependency being waited for.
- **Readiness probes must verify the export is published**, not just that nfsd is
  registered. `rpcinfo` can pass while ZFS is still importing; confirm with
  `showmount -e` before mounting.
- **Self-heal pattern for NFS-backed compose stacks:** Any stack using a docker
  `local` NFS volume needs a oneshot (ordered after docker + network-online) that
  waits for the export then re-ups, because the restart policy cannot recover a
  boot-time volume mount failure.

## Related Issues

- Issue #36 — the outage this fix resolves (branch `fix/36-boot-order-nfs`).
- [[truenas-nfs-service-down-after-crash]] — **differential diagnosis:** if the
  TrueNAS NFS service is *enabled* but dependents still fail to mount after a
  *reboot* (not a crash), suspect this boot-order inversion rather than a
  service-state problem.
- [[lms-custom-init-and-image-extension-hooks]] — the lms `/music` NFS dependency
  whose end-to-end verification (#12) was blocked by this outage.
- [[scoped-nftables-on-live-host]] — the systemd-oneshot-on-a-live-host convention
  the self-heal unit follows.

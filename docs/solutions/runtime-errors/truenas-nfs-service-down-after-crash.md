---
title: "Lyrion/NFS volume fails to mount: TrueNAS NFS service down after a Proxmox crash"
date: 2026-06-21
category: runtime-errors
module: containers
problem_type: runtime_error
component: tooling
symptoms:
  - "docker compose up fails: failed to populate volume ... connection refused (NFS mount)"
  - "showmount -e 10.99.99.2 returns clnt_create RPC Unable to receive"
  - "rpcinfo -p <truenas> shows no NFS program; tcp 111 and 2049 closed/filtered"
  - "lms (Lyrion) container stuck Exited 137 for weeks"
root_cause: config_error
resolution_type: environment_setup
severity: high
related_components:
  - truenas
  - nfs
  - lms
  - proxmox
tags:
  - truenas
  - nfs
  - lms
  - proxmox
  - crash-recovery
  - host-only-bridge
---

# Lyrion/NFS volume fails to mount: TrueNAS NFS service down after a Proxmox crash

## Problem

After an n5pro Proxmox host crash (June 3-4), VMs were killed and the TrueNAS NFS
service did not come back. Any container with an NFS volume from TrueNAS (e.g.
Lyrion's read-only `/music` over the host-only vmbr2 bridge at 10.99.99.2) fails
to start — `docker compose up` errors on volume populate with `connection
refused`. The container sits `Exited 137`. The **data is intact**; only the NFS
service is down.

## Symptoms

- `failed to populate volume lyrion_lms_music: ... addr=10.99.99.2 ... connection refused`
- `showmount -e 10.99.99.2` → `clnt_create: RPC: Unable to receive`
- `rpcinfo -p 10.99.99.2` → no NFS program registered; TCP 111 + 2049 closed.

## What Didn't Work

- **Restarting the TrueNAS VM** (or letting a playbook bounce it) does NOT restore
  NFS — the VM came up (uptime confirmed it restarted) but NFS stayed down,
  because the service's auto-start was left off by the crash.
- **Re-deploying the lms stack via Ansible** can't help — the failure is on the
  TrueNAS server side, not the client.

## Solution

The NFS *service inside TrueNAS* must be re-enabled. TrueNAS is configured via its
own UI/API, deliberately outside this repo's Ansible flow:

1. TrueNAS UI → **System Settings → Services** → **NFS**: set **Running** and
   tick **Start Automatically** (the crash left auto-start off).
2. **Shares → Unix (NFS) Shares**: confirm the music export exists and is enabled
   for the `10.99.99.0/24` host-only network.
3. Confirm TrueNAS still holds `10.99.99.2` on its vmbr2 NIC (static, manual).
4. Verify + redeploy:
   ```
   ansible n5pro_docker -m shell -a 'showmount -e 10.99.99.2' -b   # lists the export
   task deploy:service -- --tags lms --limit n5pro_docker
   ```

## Why This Works

The crash left the network path and data intact (VM reachable on 10.99.99.2, MAC
matches TrueNAS's vmbr2 NIC, ZFS data present) but the NFS daemon disabled.
Re-enabling the service with auto-start restores the export; the NFS client mount
then succeeds and dependent stacks start.

## Prevention

- In TrueNAS, ensure **Start Automatically** is enabled for the NFS service so a
  host crash/reboot brings it back unattended.
- Diagnostic shortcut for "NFS volume won't mount": from the client run
  `rpcinfo -p <server>` / `showmount -e <server>`. `RPC: Unable to receive` +
  closed 111/2049 = server NFS service down (not a network or export-permission
  problem). Reachable IP with matching MAC rules out the network path.
- Consider bringing TrueNAS NFS/export state under TrueNAS-API automation so
  crash recovery is repeatable rather than a manual UI checklist.

## Related Issues

- Issue #36 (this outage); blocks #12 (lms ffmpeg) end-to-end verification.
- [[proxmox-boot-order-inversion-breaks-nfs-volume-mount]] — **differential
  diagnosis.** Same symptom (lms can't mount `/music`), different durable cause.
  If the TrueNAS NFS service is *enabled* and serving but dependents still fail to
  mount after a *reboot* (not a crash), the culprit is Proxmox boot ordering
  (consumer LXC starting before the TrueNAS VM), not service state. Note that
  TrueNAS auto-start alone is insufficient — a reboot can still strand dependents
  if the Docker LXC boots before the TrueNAS VM finishes booting.
- [[docker-compose-shared-network-subnet-recreate]] — other Compose volume/network gotchas.

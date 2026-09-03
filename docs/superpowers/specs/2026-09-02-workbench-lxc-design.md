# Workbench LXC pattern + CT 202 `music-workbench` — design (#254)

Date: 2026-09-02. Status: implemented in the same PR.

## Goal

A short-lived, unprivileged Ubuntu 26.04 LXC on n5pro for processing music tags
over SSH with CLI tools, with read/write access to the TrueNAS music library —
and a reusable "workbench" shape: one host_vars block, one inventory line, a
package list, and a teardown command.

## Decisions

- **Unprivileged CT; the Proxmox host mounts NFS and bind-mounts it in** (chosen
  over a privileged CT mounting NFS itself, for isolation). Consequences handled:
  1. Boot-order inversion: the NAS is VM 200 on the same host, so the host can
     never mount at boot, and a bind mount taken from an empty source stays empty
     for the CT's life. A pre-start hookscript starts the `.mount` units on demand
     and refuses to start a CT whose bind sources under `/mnt/nfs/` are not live.
  2. uid mapping: CT root = host uid 100000; the library is owned by uid/gid
     3000. Handled server-side: the TrueNAS export of the dataset root to the
     host's vmbr2 address carries *Mapall* to the library owner. No `lxc.idmap`,
     no raw `.conf` edits. Files display as `nobody:nogroup` in the CT.
- `bind_mounts` is a new guest key, separate from `mounts`, so the
  fresh-allocation restore gate (#127/#148) never marks a bind source (the marker
  would land inside the NAS share). It is reconciled via `pct set`, as is the new
  `hookscript` key — both are `root@pam`-only in Proxmox.
- Teardown is a playbook that refuses while the CT is still declared (so a later
  `infra:hosts` cannot resurrect it) and when the hostname does not match.
- `nesting: true` is REQUIRED for this template: systemd 259 in an unprivileged
  CT without nesting fails every early unit with `status=243/CREDENTIALS`.

## Components

| Piece | Where |
| --- | --- |
| `proxmox_nfs_mounts` → `.mount` units (never enabled), mountpoint list, `snippets` on `local`, hookscript | `roles/proxmox_host/tasks/nfs-mounts.yml`, `files/nfs-bind-prestart.sh` |
| `ubuntu-26.04` template, `bind_mounts` + `hookscript` keys and reconcile, features reconcile for unprivileged CTs, one-key-per-line `pubkey` | `roles/proxmox_guests` |
| CT 202 declaration, `proxmox_nfs_mounts` entry | `inventory/host_vars/n5pro/vars.yml` |
| Group `workbench_hosts`, play with `common` only, `common_extra_packages` | `inventory/hosts.yml`, `playbooks/configure-guests.yml`, `roles/common` |
| Teardown | `playbooks/destroy-guest.yml`, `task infra:guest:destroy` |

## Operator-side TrueNAS steps (done once, by hand)

Shares → NFS → Add: path = the dataset root, Hosts = the Proxmox host's vmbr2
address, Advanced → Mapall User/Group = the library owner account (uid/gid 3000).
The pre-existing subdirectory export for lms is untouched.

## Verification performed (2026-09-02)

- Dry-run delta vs master: only the new artefacts.
- Live apply: CT created, binds + hookscript set before first start; second run
  `changed=0`; `configure-guests` second run `changed=0`.
- Hookscript refused a start with the NAS blackholed and with a missing bind
  source; started and mounted on demand once reachable.
- Writes from the CT arrive as 3000:3000 (seen from the lms container).
- Destroy playbook: refused a wrong hostname, destroyed the real CT, recreate
  came up with both operator keys on separate lines.

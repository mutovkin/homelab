---
title: "Add a firewall rule to a live host without lockout risk: a dedicated fail-open nftables table"
date: 2026-06-21
category: conventions
module: nut
problem_type: convention
component: tooling
severity: medium
applies_when:
  - "adding a firewall rule to a live host that has no managed firewall yet"
  - "restricting one service port to specific client IPs"
  - "the host is remote/critical and a bad rule would lock out SSH or an API"
related_components:
  - nftables
  - nut
  - proxmox
tags:
  - nftables
  - firewall
  - lockout-safety
  - defense-in-depth
  - live-host
---

# Add a firewall rule to a live host without lockout risk

## Context

n5pro (a live Proxmox host serving VMs + NFS, reached only over SSH) had no
managed firewall. We needed to restrict the NUT upsd port (3493) to one client IP
without risking SSH/Proxmox/NFS lockout — a botched default-drop rule on a remote
host is unrecoverable without console access.

## Guidance

Use a **dedicated, single-purpose nftables table with `policy accept`** that
filters **only** the one port. It physically cannot blackhole the host:

```nft
add table inet svc_fw
delete table inet svc_fw          # idempotent: nft -f applies add+delete+create atomically
table inet svc_fw {
  chain input {
    type filter hook input priority -10; policy accept;
    tcp dport != 3493 accept       # FIRST + terminal: all other traffic exits here
    iif "lo" accept
    ip saddr 127.0.0.1 accept
    ip saddr 192.168.25.5 accept   # the one allowed client
    tcp dport 3493 drop            # only this port, only non-allowed sources
  }
}
```

Why each choice removes lockout risk:
- **`tcp dport != 3493 accept` first**: a terminal verdict that every non-target
  packet (SSH 22, Proxmox 8006, NFS 2049/111) hits and exits before any `drop`.
- **`policy accept`**: the only `drop` is gated behind the target port; anything
  that falls through is accepted. A dedicated default-accept table can't blackhole.
- **Fail-open everywhere**: delete-table on stop, partial reload, or a stray
  global flush all leave the port *open*, never the host unreachable.
- **Separate table** (`inet svc_fw`): nftables.service / pve-firewall manage their
  own tables and won't clobber it; it won't clobber them.

Load it via a systemd oneshot (`enabled`, `RemainAfterExit=yes`,
`ExecStart=nft -f <file>`, `ExecStop=-nft delete table ...`) so it persists across
reboot. Template the ruleset with `validate: "/usr/sbin/nft -c -f %s"` so a
malformed rule is caught before it's written. The unit needs more than that to be
trustworthy — see [Implementation of record](#implementation-of-record-rolesnft_scoped_fw) below.

**Scope warning — docker-published ports.** This recipe's `hook input` chain only sees
traffic delivered to the host's own stack. A **docker-published** port is DNAT'd in
prerouting (`dstnat`/-100) and takes the FORWARD path, so an input-hook filter is
silently inert for it. For those ports, hook `prerouting` at a priority before -100 and
scope with `fib daddr type != local accept` — see
[nftables-input-hook-inert-for-docker-published-ports](../integration-issues/nftables-input-hook-inert-for-docker-published-ports.md),
which also hardens this doc's oneshot loader against externally flushed tables
(check-and-heal + `PartOf=nftables.service`).

## Implementation of record: `roles/nft_scoped_fw`

Since #114 this recipe is not copied per service — it is one shared role,
included (never `roles:`-listed) by each consumer:

```yaml
- name: Restrict the Portainer UI port to approved sources
  ansible.builtin.include_role:
    name: nft_scoped_fw
  vars:
    nft_fw_name: portainer          # → table inet portainer_fw,
                                    #   /etc/nftables.d/portainer-firewall.nft,
                                    #   portainer-firewall.service
    nft_fw_ports:                   # per-port allowlist
      9000: [192.168.25.20/32, 192.168.48.0/24]
```

Consumers today: `nut` (input variant), `services/portainer`,
`services/vaultwarden`, `services/postgresql` (prerouting variant).

Parameters that carry real decisions:

| Parameter | Meaning |
| --------- | ------- |
| `nft_fw_hook` | `input` (priority -10) for a **host daemon** port — upsd; `prerouting` (-150) for a **docker-published** port. Getting this wrong is silent: an input hook never sees a DNAT'd flow. |
| `nft_fw_ports` | `{port: [cidr, ...]}`. **Per-port**, deliberately: a source approved for pgAdmin's 10080 has no business on 5432. Asserted non-empty, integer keys, every source an IPv4 CIDR of prefix /1../32 — a structural check, so `0.0.0.0/0` and friends cannot sneak back in. |
| `nft_fw_scope_guard` | `fib` → `fib daddr type != local accept` (default for prerouting); `host_addr` → `ip daddr != <addr> accept` plus a daddr-scoped final drop (postgres, whose host_addr is asserted to be a real address of the host — a drifted value makes the whole filter inert); `none` for the input variant. |
| `nft_fw_lan_iface` | Adds the hairpin accept `iifname != <lan> ip saddr 172.16.0.0/12 accept`, so a container reaching the published port via the host address is admitted while a LAN packet with a **forged** 172.16/12 source still falls through to the drop. |
| `nft_fw_drop_ipv6` | Needed when the final drop is IPv4-scoped (`scope_guard: host_addr`), which IPv6 would otherwise sail past. |

### The loader hardening (#112)

Four things the original recipe lacked, now in the shared unit + tasks:

1. `PartOf=nftables.service` — a **runtime** restart of nftables.service runs
   `flush ruleset` and deletes the table while the oneshot stays
   `active (exited)`. PartOf propagates the restart; `After=` orders the reload
   behind the flush.
2. `ExecStop=-/usr/sbin/nft delete table …` — the leading `-` makes an
   already-absent table a non-failure, so the stop half of a restart cannot
   mark the unit `failed`.
3. **Probe → inline reload → hard verify.** `state: started` is a no-op on an
   already-active RemainAfterExit unit, so it can *never* heal a flushed table.
   The role probes the kernel (`nft list table inet <name>_fw`,
   `failed_when: false`, `changed_when: false`, `check_mode: false`), reloads
   when the ruleset/unit changed **or** the table is missing, then re-lists and
   fails unless the output contains `drop` — asserting the verdict, not mere
   table existence, catches a half-loaded table. A green play now means a
   LOADED table.
4. **Registers, not handlers.** The reload is inline off those registers. A
   handler notified mid-play is dropped if the play later aborts (the new
   ruleset then sits on disk unloaded forever), and handler names are global to
   a play — with the role included three times on one host they would collide
   and dedupe into a single notification.

## Why This Matters

The instinct (a default-drop chain with explicit allows) is exactly what locks
you out of a remote host. Inverting to a default-accept table that only ever
*drops one port* makes the worst case "the protection silently doesn't apply"
rather than "the host is gone." On infra you can't walk over to, fail-open is the
only safe direction for a defense-in-depth control.

## When to Apply

Any time you add the first/only firewall rule to a live, remote, or critical host
— especially to scope a single service port. Don't reach for a default-drop policy
unless you have console/IPMI fallback and a deliberate full-firewall design.

## Examples

- NUT 3493 scoped to the eq12 client (#28). Live-verified: eq12 (allowed) reaches
  upsd, n5pro_docker (not allowed) blocked, SSH/Proxmox untouched.
- Check-mode note: a systemd unit that enables/starts a file written earlier in
  the same play fails under `--check` (file not actually written) — guard the
  enable/start task and its handler with `when: not ansible_check_mode`. See
  [[ansible-change-loop-pitfalls]].

## Related

- Issue #28; builds on #9 (bind upsd to specific addresses, not 0.0.0.0).
- Issues #112 (loader hardening) and #114 (shared role, per-port allowlists,
  pg_hba templated from the same allowlist).
- [[ansible-change-loop-pitfalls]] — check-mode safety.
- [nftables-input-hook-inert-for-docker-published-ports](../integration-issues/nftables-input-hook-inert-for-docker-published-ports.md) — docker-published-port variant; self-healing loader hardening (#80, #112).

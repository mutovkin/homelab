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
`ExecStart=nft -f <file>`, `ExecStop=nft delete table ...`) so it persists across
reboot. Template the ruleset with `validate: "/usr/sbin/nft -c -f %s"` so a
malformed rule is caught before it's written.

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
- [[ansible-change-loop-pitfalls]] — check-mode safety.

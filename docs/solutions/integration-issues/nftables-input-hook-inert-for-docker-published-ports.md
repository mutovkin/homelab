---
title: "nftables input-hook rules never see docker-published ports, and a RemainAfterExit oneshot hides a flushed table from Ansible forever"
date: 2026-08-16
category: integration-issues
module: nftables
problem_type: integration_issue
component: tooling
symptoms:
  - "Portainer's plaintext-HTTP port 9000 (rw docker.sock, root-equivalent) published on 0.0.0.0 still answers from any LAN IP after an input-hook nftables allowlist is deployed and loaded"
  - "The allowlist chain's drop counter never increments while traffic demonstrably reaches the docker-published port"
  - "systemd oneshot firewall unit reports active (exited) while nft list table says the table does not exist (externally flushed by stock Debian nftables.service flush ruleset)"
  - "Every Ansible deploy is green while the firewall protection is silently absent"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - portainer
  - docker
  - systemd
  - ansible
  - debian-lxc
tags:
  - nftables
  - docker
  - prerouting
  - dnat
  - portainer
  - systemd-oneshot
  - firewall
  - fail-open
---

# nftables input-hook rules never see docker-published ports, and a RemainAfterExit oneshot hides a flushed table from Ansible forever

## Problem

Portainer's admin UI — plaintext HTTP in front of a **read-write `docker.sock`** — was
published on `0.0.0.0:9000` on both docker LXCs (eq12_docker 192.168.25.15 and
n5pro_docker 192.168.30.15). Anyone on the LAN who could reach port 9000 had a path to
root-equivalent control of the Docker host (issue #80).

The remediation was to apply the repo's existing convention
([scoped-nftables-on-live-host](../conventions/scoped-nftables-on-live-host.md)):
a dedicated, single-purpose, **fail-open** nftables table that drops only the one port
for non-allowlisted sources. But the convention's recipe — proven correct for NUT's
`upsd` — hooks the **input** chain, and for a *docker-published* port that recipe is
**silently inert**: the table loads cleanly, `nft list table` looks right, and the port
stays wide open. Worse, the "obvious" correction (move the same drop into prerouting)
has a second silent-failure trap that would have broken container *egress* to any
external host's port 9000. Both traps produce zero errors; only live verification from
a blocked source exposes them.

Further traps live on the deploy side: the loader unit is a
`RemainAfterExit=yes` oneshot, so systemd reports it "active" even after the table has
been externally flushed (`state: started` can never heal it), and Debian's stock
`nftables.service` opens each load with `flush ruleset` — so an external flush of the
protection is a *routine* event, not a hypothetical.

## Symptoms

- Portainer UI reachable over plaintext HTTP from any LAN address on tcp/9000, on both
  docker LXCs, with the container holding a read-write `/var/run/docker.sock`.
- With the convention's `hook input` chain loaded: the ruleset applies without error and
  is visible in `nft list ruleset`, but a curl from a non-allowlisted LAN source still
  reaches the UI — the drop rule never matches IPv4 LAN traffic.
- After an external `flush ruleset` (e.g. an `nftables.service` restart):
  `systemctl status portainer-firewall` still shows `active (exited)` while
  `nft list table inet portainer_fw` fails — the protection is gone and nothing reports it.

## What Didn't Work

### (a) The convention's `hook input` recipe — silently inert for DNAT'd traffic

The prior convention's ruleset filters in the input hook
([scoped-nftables-on-live-host](../conventions/scoped-nftables-on-live-host.md),
mirrored live in `ansible/roles/nut/templates/nut-firewall.nft.j2`):

```nft
chain input {
  type filter hook input priority -10; policy accept;
  tcp dport != 3493 accept
  ...
  tcp dport 3493 drop
}
```

For a docker-**published** port this never matches the traffic it is meant to block.
Docker publishes ports with a DNAT rule in **prerouting at priority `dstnat` (-100)**:
an inbound LAN packet to `192.168.25.15:9000` is rewritten to the container IP before
routing, then traverses the **forward** path into the bridge. It is never delivered to
the host's local stack, so an input-hook chain literally never sees it. The table loads,
looks correct, and filters nothing — with `policy accept` everywhere there is no error,
no log, no counter movement.

Important nuance: the input-hook recipe **remains correct for host daemons**. NUT's
`upsd` binds directly on the host, its traffic genuinely arrives via the input hook, and
`ansible/roles/nut/templates/nut-firewall.nft.j2` should stay exactly as it is (parity
hardening for nut is tracked separately in follow-up #112). The lesson is not "input
hooks are wrong" — it is that **the correct hook depends on whether the socket is a host
listener or a docker-published (DNAT'd) port**.

### (b) Naive prerouting drop without a `fib` guard — a container-egress trap

Moving the same allowlist-and-drop into `hook prerouting priority -150` fixes visibility
but introduces a subtler failure: prerouting sees **every packet entering the host**,
including forwarded traffic — which on a docker host includes **container egress**. A
container connecting outbound to *any external* `<host>:9000` produces a packet entering
prerouting with `tcp dport 9000` and a non-allowlisted source (a `172.x` container IP),
and the naive rule drops it. That breaks outbound connections that have nothing to do
with Portainer, silently, and only for destinations that happen to use port 9000.

This one was caught in code review, not observed live — a latent trap, which is exactly
why it earns a rule with its own comment in the template
(`ansible/roles/services/portainer/templates/portainer-firewall.nft.j2`):
`fib daddr type != local accept` exits any flow not addressed to this host, and it works
precisely *because* the chain runs at -150, **before** DNAT rewrites the destination —
at that point inbound-to-Portainer traffic still carries the host's LAN IP (local),
while container egress / east-west / forwarded flows carry a foreign daddr (non-local).

### (c) `state: started` as the heal mechanism — impossible by construction

The loader unit is `Type=oneshot` + `RemainAfterExit=yes`
(`ansible/roles/services/portainer/templates/portainer-firewall.service.j2`).
After its one successful `nft -f`, systemd considers it `active (exited)` forever —
including after something else deletes or flushes the table. Ansible's
`systemd: state: started` sees "already active" and does nothing. As a drift-healing
mechanism it cannot work even in principle: the unit's activeness is a statement about
the *past exec*, not about whether the table currently exists in the kernel. And the
drift scenario is real: the stock Debian `/etc/nftables.conf` on both hosts begins with
`flush ruleset`, and `nftables.service` is enabled and active — any restart of it wipes
`inet portainer_fw` while `portainer-firewall.service` stays "active".

### (d) Relying on end-of-play handlers — an aborted play strands the drift forever

The first-draft wiring was the idiomatic one: template notifies handler, handler runs at
end of play. But if the play aborts after the template task writes the file and before
handlers flush, the changed ruleset sits on disk **unloaded** — and on every subsequent
run the `template` task compares against the already-current file, reports `ok`, and
**never notifies again**. The kernel ruleset and the on-disk ruleset disagree
permanently, with every future run green. The fix is to stop trusting the notify chain
as the only trigger: probe the kernel directly every run and treat "table missing" as a
change (see the check-and-heal sequence below).

## Solution

The #80 PR adds a firewall stage to the portainer service role, live-verified on both
docker LXCs.

**The ruleset shape**
(`ansible/roles/services/portainer/templates/portainer-firewall.nft.j2`; port and
allowlist from `ansible/roles/services/portainer/defaults/main.yml` — 9000, allowed:
192.168.25.20 (NPM) and 192.168.48.0/24 (operator subnet)):

```nft
add table inet portainer_fw
delete table inet portainer_fw          # add+delete+create in one nft -f = atomic, idempotent
table inet portainer_fw {
  chain prerouting {
    type filter hook prerouting priority -150; policy accept;
    # All other TCP traffic exits here (terminal accept); non-TCP falls through to policy accept.
    tcp dport != 9000 accept
    # In scope only when addressed to this host: at -150 daddr is still the host
    # LAN IP (pre-DNAT), while container egress / east-west / other forwarded
    # flows to some external :9000 are non-local and exit here.
    fib daddr type != local accept
    iif "lo" accept
    ip saddr 127.0.0.1 accept
    ip saddr 192.168.25.20 accept       # templated from portainer_fw_allowed_sources
    ip saddr 192.168.48.0/24 accept
    # Any other source reaching the Portainer port is dropped (IPv4 and IPv6).
    tcp dport 9000 drop
  }
}
```

Rule order is the safety argument: **fail-open** `policy accept`; the terminal
`tcp dport != 9000 accept` **first** so SSH and every other service exits before any
drop can apply; `fib daddr type != local accept` **second** to scope the chain to
host-addressed flows; then loopback + allowlist accepts; then a **single-port** drop.
The allowlist is IPv4-only and the final drop matches IPv6 too, so external IPv6 access
to 9000 is dropped by design.

**The loader unit**
(`ansible/roles/services/portainer/templates/portainer-firewall.service.j2`): a
`oneshot`/`RemainAfterExit` unit with `ExecStart=nft -f` and
`ExecStop=-nft delete table inet portainer_fw`, ordered `After=nftables.service` (whose
`flush ruleset` would otherwise wipe it at boot) and `Before=docker.service` (rules live
before ports publish), plus — the key addition — **`PartOf=nftables.service`**:
`After=` alone only covers *boot* ordering; `PartOf=` makes a **runtime**
`systemctl restart nftables` propagate a restart to this unit, so the routine
flush-ruleset event re-loads the table automatically.

**The Ansible check-and-heal sequence**
(`ansible/roles/services/portainer/tasks/main.yml`, handler in
`ansible/roles/services/portainer/handlers/main.yml`):

```yaml
- name: Deploy Portainer firewall ruleset
  ansible.builtin.template:
    src: portainer-firewall.nft.j2
    dest: /etc/nftables.d/portainer-firewall.nft
    mode: "0644"
    validate: "/usr/sbin/nft -c -f %s"       # malformed rule caught before it lands
  notify: reload-portainer-firewall

# ... unit template (also notifies), enable+start (guarded when: not ansible_check_mode) ...

- name: Flush handlers so firewall changes are live before the compose deploy
  ansible.builtin.meta: flush_handlers

# A RemainAfterExit oneshot stays "active" even if the table was externally
# flushed, so state: started can never heal it. Detect drift and notify.
- name: Check Portainer firewall table is live
  ansible.builtin.command: /usr/sbin/nft list table inet portainer_fw
  register: portainer_fw_check
  changed_when: portainer_fw_check.rc != 0
  failed_when: false
  notify: reload-portainer-firewall
  when: not ansible_check_mode

- name: Heal a missing firewall table immediately
  ansible.builtin.meta: flush_handlers

- name: Assert Portainer firewall table is live
  ansible.builtin.command: /usr/sbin/nft list table inet portainer_fw
  changed_when: false
  when: not ansible_check_mode
```

The handler restarts `portainer-firewall.service` (a restart re-runs `ExecStart`, which
atomically re-creates the table). The first `flush_handlers` makes template changes live
*before* the compose deploy publishes the port; the check task then asks the **kernel**
(not systemd, not the notify chain) whether the table exists, converts absence into a
change + notification, the second `flush_handlers` heals immediately, and the final
bare `nft list table` is a hard assert — if the heal didn't take, the play fails loudly
instead of ending green.

## Why This Works

- **Prerouting at -150 sees every inbound path.** Hooking before Docker's DNAT
  (`dstnat` = -100) means the chain matches the *original* dport and the *original*
  (host-local) daddr on both real paths: the DNAT/forward path that serves IPv4 LAN
  clients, and the docker-proxy input path that serves the `[::]` listener. The input
  hook can never offer this for DNAT'd traffic — the packets take the forward path
  around it.
- **The `fib` rule is what makes prerouting safe.** Prerouting's breadth (it sees
  forwarded and container-egress flows too) is tamed by exiting every non-host-addressed
  flow before the allowlist logic — evaluated pre-DNAT, so "addressed to this host"
  still means the LAN IP for inbound Portainer traffic.
- **Fail-open by construction.** `policy accept`, a terminal accept for every other
  dport first, and a drop gated on the single port mean the worst case of any failure
  (unit stop, table delete, stock `flush ruleset`) is "the port is open again" — never
  "the host is unreachable". This preserves the prior convention's lockout-safety
  property while fixing its hook.
- **Drift cannot survive a deploy.** The kernel-probe check task fires the handler on a
  missing table regardless of file state, immediate `flush_handlers` closes the
  aborted-play window, `PartOf=nftables.service` closes the runtime-restart window
  without any deploy at all, and the hard assert turns any remaining gap into a red run.
- **Live-verified on both hosts.** With the chain at `hook prerouting priority -150`:
  a blocked LAN source (the Proxmox host) times out (curl exit 28) while the allowlisted
  sources (192.168.25.20 NPM, 192.168.48.0/24 operator subnet) reach the UI; collateral
  published services (8086, 18080, 9001) are unaffected. The heal path was proven live
  on eq12: `nft delete table inet portainer_fw` (simulating an external flush) left the
  unit "active"; the next deploy's check task reported changed, the handler reloaded the
  table, the assert passed (recap `changed=2 failed=0`), and the following deploy was
  `changed=0`. Second applies are `changed=0` on both hosts.

## Prevention

- **Pick the hook by socket type, not by recipe.** Host daemon binding the host stack
  (nut's `upsd`, sshd) → `hook input` per the existing convention. Docker-**published**
  port → `hook prerouting` at a priority **before** `dstnat` (-100); we use -150. An
  input-hook rule for a DNAT'd port is not wrong-but-degraded, it is *invisible* — it
  will pass every "does the ruleset load" check and block nothing.
- **Any prerouting filter needs a scope guard.** Pair the port match with
  `fib daddr type != local accept` (pre-DNAT) so container egress and forwarded east-west
  traffic to the same port number elsewhere is explicitly out of scope. Without it the
  failure is again silent, and it lands on traffic unrelated to the service you're
  protecting.
- **Never trust systemd unit state as proof a `RemainAfterExit` oneshot's *effect* is
  live.** Probe the actual kernel/system state (`nft list table ...`) every run, convert
  absence into `changed` + notify, `flush_handlers` immediately, and end with a hard
  assert. End-of-play handlers alone leave a permanent silent gap after any aborted play.
- **Use `PartOf=` in addition to `After=` when a stock service's restart destroys your
  state.** `After=nftables.service` orders boot; `PartOf=nftables.service` re-runs the
  loader on a runtime nftables restart. The stock Debian `nftables.conf` starts with
  `flush ruleset` and the service is enabled+active on these hosts — the external-flush
  scenario is routine, not theoretical.
- **Verify firewall changes from a blocked source, not just an allowed one.** The only
  test that distinguishes "inert" from "working" is a connection attempt that *should*
  fail (expect curl exit 28), alongside allowed-source success and a spot-check that
  collateral published ports still answer. This is the firewall instance of the repo's
  standing rule: verify with the subsystem's own behavior, not with green module output.

## Related Issues

- Issue #80 — this fix (Portainer exposure), shipped in the #80 PR.
- Issue #112 — follow-up: bring the nut firewall up to the same self-healing standard
  (check-and-heal + `PartOf=`); its input hook itself is correct and stays.
- Issue #114 — proposes extracting a shared scoped-firewall pattern from this and the
  nut implementation.
- Issue #84 — docker-network renumbering; any future subnet-scoped rules must treat the
  pinned host_vars subnets as authoritative until it lands.
- [scoped-nftables-on-live-host](../conventions/scoped-nftables-on-live-host.md) — the
  fail-open convention this doc refines: its recipe applies to host-daemon ports; for
  docker-published ports use this doc's prerouting variant.
- [ansible-change-loop-pitfalls](../conventions/ansible-change-loop-pitfalls.md) —
  check-mode guards for same-play-written systemd units; assert-the-end-state
  discipline that the check-and-heal sequence instantiates.
- [compose-up-recreates-watchtower-created-containers](compose-up-recreates-watchtower-created-containers.md)
  — expected recreate noise when deploying against portainer.

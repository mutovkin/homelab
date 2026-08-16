---
title: "Allowlisting a docker-published port with same-bridge consumers: daddr/iif scoping, a no-defaults allowlist contract, and peer-auth probe noise"
date: 2026-08-16
category: conventions
module: services/postgresql
problem_type: convention
component: tooling
severity: high
applies_when:
  - "firewalling a docker-published port whose service also has same-bridge container consumers (shared docker network)"
  - "containers reach the published port via the host address (hairpin) and must keep working"
  - "defining the allowlist contract for a per-service firewall (host_vars, asserts)"
  - "switching postgres pg_hba local auth from trust to peer"
symptoms:
  - "a dport-keyed prerouting drop kills container-to-container traffic on the shared bridge (br_netfilter passes bridged frames through the hook)"
  - "a fib-only scope guard silently drops legitimate hairpin connections from local containers to the host-published port"
  - "~2900 FATAL peer-auth entries per day in postgres logs from a root-run compose healthcheck, while pg_isready still exits 0 and the container stays healthy"
related_components:
  - nftables
  - docker
  - br_netfilter
  - postgresql
  - systemd
tags:
  - nftables
  - docker
  - br-netfilter
  - hairpin
  - fail-open
  - allowlist
  - peer-auth
  - silent-failure
---

# Allowlisting a docker-published port with same-bridge consumers: scoping, contract, and probe traps

## Context

Postgres (5432) and pgAdmin (10080) on deb-docker (192.168.25.15) were published on
`0.0.0.0` **and** `[::]`, with a `pg_hba.conf` `0.0.0.0/0` catch-all — any LAN device
could attempt authentication against a superuser-capable database (issue #79, P1).
Loopback binding was off the table (the NPM reverse proxy lives in a separate LXC and
needs LAN reach), so the fix is a scoped fail-open nftables allowlist on the host.

The core mechanism — why `hook input` is silently inert for docker-published ports and
the table must hook `prerouting` at priority -150 (before Docker's `dstnat` at -100),
plus the `RemainAfterExit`-oneshot/`flush ruleset` self-heal machinery — is documented
by the sibling #80 fix in
[[nftables-input-hook-inert-for-docker-published-ports]] and the CLAUDE.md Gotcha it
added. **Read that first.** This doc records what the postgres instance (#79) adds on
top: the scoping needed when the guarded service **also has same-bridge container
consumers and hairpin traffic**, the host_vars allowlist contract that fails loudly
instead of deploying unprotected, and the peer-auth probe trap that comes with
tightening the socket. It builds on the #78 config-ownership machinery
([[postgresql-mounted-configs-never-deployed-or-read]]).

The rendered postgres ruleset
(`ansible/roles/services/postgresql/templates/postgres-firewall.nft.j2` against
eq12_docker's host_vars):

```nft
table inet postgres_fw {
  chain prerouting {
    type filter hook prerouting priority -150; policy accept;
    tcp dport != { 5432, 10080 } accept   # FIRST + terminal: everything else exits here
    iif "lo" accept
    ip daddr != 192.168.25.15 accept      # br_netfilter: bridged frames hit this hook too
    ip saddr 127.0.0.1 accept
    iifname != "eth0" ip saddr 172.16.0.0/12 accept   # hairpin, iif-scoped vs. forged saddr
    ip saddr 192.168.25.20/32 accept      # NPM LXC
    ip saddr 192.168.48.0/24 accept       # operator subnet
    meta nfproto ipv6 tcp dport { 5432, 10080 } drop  # no IPv6 allowlist — [::] publish + ULA
    ip daddr 192.168.25.15 tcp dport { 5432, 10080 } drop
  }
}
```

## Guidance

### Same-bridge consumers force destination scoping — and hairpin forces a bridge accept

Docker enables `br_netfilter`, so **container↔container bridge frames traverse the
prerouting hook too**. A drop keyed only on dport would kill joplin → postgres on
`postgres_network` (172.21.0.0/24) — the live near-miss here. Every drop must be
scoped to traffic actually addressed to the host: the postgres table uses an explicit
`ip daddr != <host LAN addr> accept` early exit plus daddr-scoped drops; the portainer
table achieves the same with `fib daddr type != local accept`. Either way, bridge-
internal traffic exits before any allowlist logic.

Destination scoping alone is **not** enough when local containers legitimately reach
the published port **via the host address** (hairpin): that traffic *is* addressed to
the host, so it falls through to the allowlist — and a fib-only or daddr-only chain
with no bridge accept silently drops it. The postgres table therefore carries an
explicit hairpin accept for the docker ranges, and that accept must be
**interface-scoped**:

```nft
iifname != "eth0" ip saddr 172.16.0.0/12 accept
```

Real hairpin traffic arrives on a docker bridge interface, never on the LAN iface; a
LAN packet with a *forged* 172.16/12 source arrives on `eth0` and falls through to the
allowlist/drop instead. Without the `iifname` scope, the hairpin accept is a
LAN-spoofable hole in the allowlist; without the hairpin accept, container hairpin
breaks silently. (Portainer has no container consumers, which is why its chain safely
omits the bridge accept — check which shape your service needs before copying either.)

### The allowlist is a host_vars contract that fails loudly

The allowlist lives in host_vars (`ansible/inventory/host_vars/eq12_docker/vars.yml`,
the `postgres_firewall` dict: `host_addr`, `lan_iface`, `ports`, `allowed_sources`)
with deliberately **no role defaults** — a host deploying the role without defining it
fails loudly rather than deploying unprotected. The role's first task
(`ansible/roles/services/postgresql/tasks/main.yml`) asserts:

- the list is non-empty;
- every entry is *structurally* an IPv4 CIDR with prefix `/1../32` — a regex shape
  check, not a spelling blocklist, so `/0`, `0/0`, and malformed entries a blocklist
  would wave through are all rejected (out-of-range octets are caught downstream by
  the template's `nft -c -f` validate);
- `host_addr` is one of the host's real addresses
  (`in ansible_all_ipv4_addresses`) — a drifted `host_addr` would silently no-op the
  entire filter via the `ip daddr != … accept` early exit.

Parity rule: the pg_hba LAN entries and `postgres_firewall.allowed_sources` list the
same sources (`ansible/roles/services/postgresql/files/config/pg_hba.conf` ↔
`ansible/inventory/host_vars/eq12_docker/vars.yml`). A future off-host consumer
arrives SNAT'd as its docker host's LAN IP and must be added to **both** (enforcing
this mechanically is tracked in #114).

The postgres role's self-heal against external `flush ruleset` follows the #80 doc's
pattern with an inline-condition variant: registered template results plus a kernel
probe drive one reload task (`postgres_fw_ruleset is changed or … or 'drop' not in
probe stdout`), followed by a hard verify that fails the play unless the loaded table
contains its `drop` verdict — asserting on the drop *verdict*, not mere table
existence, catches a half-loaded table. Live-proven the same way: delete the table,
re-run, exactly one changed task, table restored, next run `changed=0`.

### Peer auth turns every root-run probe into log spam — pg_isready won't tell you

Closing the socket hole meant switching pg_hba's `local all all trust` to `peer`
(`ansible/roles/services/postgresql/files/config/pg_hba.conf`) — a plain root
`docker exec … psql` no longer gets a passwordless superuser session. But peer
requires OS user == DB role, so every *root*-run socket probe now logs
`FATAL: Peer authentication failed`. The compose healthcheck at a 30s interval was
writing **~2900 FATALs/day** into `PGDATA/log` — while `pg_isready` still exited 0
(PQping ignores the auth outcome), so the container stayed "healthy" and nothing
looked wrong except the flooded server log. Fixes, both in the tree:

- Healthcheck runs as the postgres OS user —
  `test: ["CMD-SHELL", "gosu postgres pg_isready"]` in
  `ansible/roles/services/postgresql/files/compose.yaml` (`gosu` ships in the
  official postgres image).
- The post-restart readiness gate sets `user: postgres` on
  `community.docker.docker_container_exec`
  (`ansible/roles/services/postgresql/tasks/main.yml`).

Corollary for operations: **all** admin/backup invocations become
`docker exec -u postgres …`. A root `docker exec postgres pg_dumpall` now *fails* —
and because backup output is typically shell-redirected, the failure would surface
as an **empty backup file discovered at restore time**. Update any cron/runbook
invocations in the same change that flips `trust` → `peer`.

## Why This Matters

Each trap is a control that *looks* present while being absent or over-broad: an
unscoped prerouting drop breaks in-bridge consumers; a fib-only chain silently drops
hairpin; an unscoped hairpin accept is a spoofable bypass; a drifted `host_addr`
no-ops the whole filter with a green play; a peer-auth probe floods the log while
health stays green (or, for backups, silently produces empty files). The common cure
is the #78 discipline: don't trust that a control is wired — make the role verify the
effect (drop verdict in `nft list`, `pg_isready` as the right user, asserts on the
allowlist's shape and the host address) and fail loudly when the effect is missing.

## When to Apply

- Any docker-published port getting an nftables allowlist **where the service also has
  container consumers on a shared bridge or hairpin clients** — pick the scoping shape
  deliberately (explicit daddr + iif-scoped bridge accept here; fib-only where no
  container consumers exist, as in [[nftables-input-hook-inert-for-docker-published-ports]]).
- Any per-service firewall role: define the allowlist as a no-defaults host_vars
  contract with structural asserts (CIDR shape, host_addr reality check).
- Any switch of pg_hba `local` auth to `peer` (or any auth tightening): audit every
  non-interactive socket client — healthchecks, readiness gates, backups, cron — for
  the user they run as, and remember `pg_isready` exit codes are auth-blind.

## Examples

- #79 live verification on deb-docker (192.168.25.15): NPM LXC and operator subnet
  reach 5432/10080; other LAN sources and the IPv6 path time out; joplin → postgres
  over `postgres_network` unaffected; delete-table recovery converges in one reload;
  postgres server log free of healthcheck FATALs after the `gosu` change.
- Verify from a **disallowed** client, not from the host — and remember that a
  successful or failed connection proves nothing about pg_hba scoping while an
  any-source catch-all rule still exists, since every client matches the catch-all
  anyway (session history, #78).
- Parity requirement in practice: `192.168.25.20/32` (NPM) and `192.168.48.0/24`
  (operators) appear identically in `pg_hba.conf` and
  `postgres_firewall.allowed_sources`.

## Related

- Issue #79 (this fix); #78 config ownership
  ([[postgresql-mounted-configs-never-deployed-or-read]]); #80 the sibling portainer
  fix; #112 nut-firewall self-heal parity; #114 follow-ups (parity enforcement,
  per-port allowlists, shared firewall role).
- [[nftables-input-hook-inert-for-docker-published-ports]] — the core mechanism
  (input-hook inertness, prerouting-before-dstnat, `fib` scope guard, kernel-probe
  self-heal, verify-from-blocked-source). This doc is its companion for services with
  same-bridge consumers, plus the allowlist contract and peer-auth traps.
- [[scoped-nftables-on-live-host]] — the original input-hook fail-open convention;
  correct for host-terminated ports only.
- [[unattended-upgrades-silently-inert-fleet-wide]] — the same "green play over a
  dead control" failure class.

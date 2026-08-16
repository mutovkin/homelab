# PostgreSQL Database Server

## Description

[PostgreSQL](https://www.postgresql.org/) is a powerful, open-source object-relational database system. This setup includes [PostgreSQL 18](https://www.postgresql.org/docs/18/index.html) with [pgAdmin](https://www.pgadmin.org/) 4 as a web-based administration interface. PostgreSQL serves as the central database for multiple services in the homelab including Joplin Server and other applications requiring reliable data storage.

Key features:

- ACID-compliant relational database
- Advanced SQL features and extensibility
- Multi-version concurrency control (MVCC)
- Full-text search capabilities
- JSON and JSONB data types
- Robust backup and recovery tools
- pgAdmin web interface for database management

## Configuration ownership

All bind-mounted directories and config files are created and deployed by this role
(`tasks/main.yml`) — edit them in the repo, never on the host (#78):

| Repo path | Host path (mounted into the container) | Paired action on change |
| --------- | -------------------------------------- | ----------------------- |
| `files/config/postgresql.conf` | `/data/postgresql/config/postgresql.conf` | restarts `postgres` (client-visible outage) |
| `files/config/pg_hba.conf` | `/data/postgresql/config/pg_hba.conf` | restarts `postgres` (client-visible outage) |
| `files/config/pgadmin/servers.json` | `/data/postgresql/config/pgadmin/servers.json` | none — imported only on first pgAdmin launch (fresh `/var/lib/pgadmin`) or with `PGADMIN_REPLACE_SERVERS_ON_STARTUP=True` (not set); on an initialized install, change servers in the UI |
| `templates/01-init-databases.sql.j2` | `/data/postgresql/init-scripts/01-init-databases.sql` | none — init scripts run only on a fresh initdb |

`postgresql.conf` sets `hba_file = '/etc/postgresql/pg_hba.conf'` so the mounted
`pg_hba.conf` is actually read (without it PostgreSQL falls back to the copy in
`$PGDATA` and the mount is silently ignored). The restart is the conservative blanket
action: several `postgresql.conf` params are restart-only and the initial `hba_file`
cutover needs one. A `pg_hba.conf` content edit on its own would strictly only need a
reload — a possible future refinement.

The restart only ever bounces a container that already existed before the run. On a
fresh host `compose up` creates `postgres` from the configs deployed by this role, and
restarting it seconds later would race the entrypoint's `initdb`, leaving a cluster
that skips `/docker-entrypoint-initdb.d` forever.

The init script is a template rendering `vault_joplin_postgres_password` from
`ansible/inventory/host_vars/eq12_docker/vault.yml`; it is written `999:999` mode
`0600` so the credential is not world-readable on the host, and is deployed with
`diff: false` so a `--diff` run never prints it.

`config/` is excluded from the deploy-dir rsync payload — the configs go straight to
their container mount paths, so a second copy under `/data/deploy/postgresql/` would
just be decorative drift.

## Network exposure (#79)

Compose publishes `5432` (PostgreSQL) and `10080` (pgAdmin) on the host, so both were
reachable from the whole LAN — and, because the LXC has a LAN-routable ULA (`fd…`,
`scope global` in `ip addr`), over IPv6 too. The ports stay published (Nginx Proxy
Manager runs in a *separate* LXC, CT 104, so a `127.0.0.1:` bind is not an option);
reachability is scoped by a host firewall instead.

**Why PREROUTING and not `hook input`.** Docker DNATs published ports in `PREROUTING`
(its `dstnat` chain, priority `-100`). DNAT'd flows are rewritten to the container IP and
take FORWARD — an input-hook chain (the `nut` role's convention) never sees them; only
residual `docker-proxy` traffic reaches INPUT (still on the original port). PREROUTING
before `dstnat` (`-100`) is the one place both paths are visible on the original dport,
so this table hooks `prerouting` at priority `-150`.

**Fail-open by construction** (see
`docs/solutions/conventions/scoped-nftables-on-live-host.md`): a single-purpose table
(`inet postgres_fw`) with `policy accept`; the first rule is terminal for every **TCP**
packet not aimed at the two published ports (nft gives it an implicit `l4proto tcp`
dependency, so non-TCP traffic falls through to the policy accept instead — fail-open
either way); only those ports are ever dropped; unloading the table (`ExecStop`, a stray
flush) leaves the ports **open**, never the host unreachable. The ruleset is validated
with `nft -c -f` at template time before it is written.

| Artifact | Purpose |
| -------- | ------- |
| `templates/postgres-firewall.nft.j2` | ruleset → `/etc/nftables.d/postgres-firewall.nft` |
| `templates/postgres-firewall.service.j2` | oneshot unit → `/etc/systemd/system/postgres-firewall.service` (`RemainAfterExit`, `PartOf=nftables.service`, loads at boot before `docker.service`, ordering only) |
| `tasks/main.yml` probe → reload → verify | converge the *loaded* table, not just the files on disk (see below) |

**Keeping the table actually loaded.** `/etc/nftables.conf` opens with `flush ruleset`,
so any restart of the enabled `nftables.service` — an unattended-upgrades package upgrade,
say — silently deletes `inet postgres_fw`. `RemainAfterExit` means our unit still reports
`active`, so `state: started` would happily no-op over a wiped firewall. Two mechanisms
close that: the unit's `PartOf=nftables.service` (an `nftables.service` restart propagates
here and, via `After=`, re-applies the table after the flush), and a probe/reload/verify
sequence in the role — a read-only `nft list table inet postgres_fw` probe
(`check_mode: false`), a reload that fires when either template changed **or** the probe
found no table, and a final verify that fails the play unless the loaded table contains
its `drop` verdict. A green play therefore means a loaded firewall, and steady state is
still idempotent (probe ok → reload skipped → verify ok, `changed=0`). `ExecStop` is
best-effort (`-`) so an already-absent table cannot fail the stop half of a restart.

The allowlist comes from the **`postgres_firewall` host_vars dict** — deliberately with
no role defaults, so a host that deploys this role without defining it fails loudly
rather than silently deploying unprotected:

```yaml
postgres_firewall:
  host_addr: 192.168.25.15
  lan_iface: eth0  # the LXC's LAN-facing interface (docker bridges are br-*/docker0)
  ports: [5432, 10080]
  allowed_sources:
    - 192.168.25.20/32  # NPM LXC (CT 104)
    - 192.168.48.0/24   # operator workstation subnet
```

An assert task validates it *structurally* rather than against a blocklist of wildcard
spellings (which would wave through `192.168.0.0/0`, `0/0`, `0.0.0.00/0`): the list must
be non-empty and every entry an IPv4 CIDR with a `/1`–`/32` prefix. It also requires
`host_addr` to be one of the host's real IPv4 addresses — a drifted value would silently
no-op the entire filter, since every packet would exit at the `ip daddr != host_addr
accept` rule.

Always allowed regardless of the allowlist: loopback (`iif lo`, `127.0.0.1`) and the
docker bridge ranges `172.16.0.0/12` (a container reaching a published port via the host
address — hairpin). That hairpin accept is interface-scoped
(`iifname != "{{ lan_iface }}"`): real hairpin traffic arrives on a docker bridge, never
on the LAN interface, so a LAN packet with a **forged** `172.16/12` source cannot use it
to skip the allowlist. Because `br_netfilter` is on, bridged container↔container frames
traverse this hook too, so the rules are scoped by destination address: anything not
addressed to `host_addr` (e.g. joplin → postgres on `172.21.0.0/24`) exits early and is
untouched. There is no IPv6 allowlist, so IPv6 traffic to these two ports is dropped
wholesale; add `ip6 saddr` accepts above that rule if an IPv6 client is ever needed.

**Authentication posture** (defense in depth, kept in parity with the allowlist):

- `local all all peer` on the Unix socket — a root `docker exec` shell no longer gets a
  passwordless superuser session. Admin and backup sessions must run as the postgres OS
  user: `docker exec -u postgres postgres psql …`, `docker exec -u postgres postgres
  pg_dumpall …`. First-boot `initdb` is unaffected (the image entrypoint already runs
  its socket `psql` as uid 999).
- **Anything probing the socket must run as `postgres` too.** `pg_isready`'s `PQping`
  ignores the auth outcome, so it exits 0 either way — but a probe running as root logs
  a `FATAL: Peer authentication failed` line on *every* attempt. With a 30s healthcheck
  that is ~2900 FATALs/day of pure noise in the server log. Hence the compose healthcheck
  is `gosu postgres pg_isready` (gosu ships in the official image) and the role's
  readiness gate passes `user: postgres` to `docker_container_exec`.
- TCP is `scram-sha-256` everywhere. The old `0.0.0.0/0` catch-all is gone; LAN entries
  now list exactly the allowlisted sources. Adding a new off-host consumer means editing
  **both** `files/config/pg_hba.conf` and `postgres_firewall.allowed_sources`.
- `listen_addresses = '*'` stays. Inside the container it binds only the container's own
  interfaces (loopback + its `172.21.0.0/24` address); a narrower value would hardcode a
  dynamic bridge IP. Exposure is controlled at the publish + nftables layer, not here.

**`PGLADMIN_CONFIG_SERVER_MODE: 'False'` was removed, not corrected.** The variable was
misspelled (`PGL…`) and therefore inert, so live pgAdmin has always run in its default
`SERVER_MODE=True` — login required. Fixing the spelling would have switched it to
desktop mode and *removed* authentication from a LAN-reachable UI, the opposite of what
this change is for. Deleting the line freezes the correct live behavior.

**Deliberately deferred:** fronting pgAdmin with NPM/TLS. `10080` is still plaintext
HTTP, now reachable only from the allowlisted sources.

### Standalone (non-Ansible) use

Place the config files yourself before `docker compose up` (paths below are relative to
this role directory, `ansible/roles/services/postgresql/`):

```bash
sudo mkdir -p /data/postgresql/{data,config/pgadmin,init-scripts}
sudo cp files/config/postgresql.conf files/config/pg_hba.conf /data/postgresql/config/
sudo cp files/config/pgadmin/servers.json /data/postgresql/config/pgadmin/
# render templates/01-init-databases.sql.j2 with your own password, then:
#   sudo install -o 999 -g 999 -m 0600 01-init-databases.sql /data/postgresql/init-scripts/

# pgAdmin writes as UID 5050 and crash-loops if Docker auto-creates this root-owned
sudo mkdir -p /data/postgresql/pgadmin
sudo chown 5050:5050 /data/postgresql/pgadmin
```

Standalone use also needs a `.env` next to `compose.yaml` supplying `POSTGRES_PASSWORD`,
`PGADMIN_EMAIL`, `PGADMIN_PASSWORD` and `TIMEZONE` (Ansible templates it from
`templates/env.j2`), plus the pre-created external `postgres_network` — see the comment
at the bottom of `files/compose.yaml`.

Standalone use gets **no firewall**: the `postgres_fw` nftables table and its systemd
unit are deployed by this role only. Published `5432`/`10080` are then reachable from
the whole network — restrict them yourself (see [Network exposure](#network-exposure-79))
before exposing the host.

## Important Notes

- **PostgreSQL data**: UID 999 (postgres user in container)
- **pgAdmin data**: UID 5050 (pgadmin user in container)
- **Config files**: Read-only mounts, owned by root (exception: the rendered init script
  is `999:999` mode `0600` — it carries a real credential)
- **Init scripts**: Executed only on first database initialization

Ensure adequate disk space as databases can grow significantly over time. The backup directory should also have sufficient space for automated backups.

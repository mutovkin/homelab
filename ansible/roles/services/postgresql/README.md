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
| `files/config/pgadmin/servers.json` | `/data/postgresql/config/pgadmin/servers.json` | restarts `pgadmin4` |
| `templates/01-init-databases.sql.j2` | `/data/postgresql/init-scripts/01-init-databases.sql` | none — init scripts run only on a fresh initdb |

`postgresql.conf` sets `hba_file = '/etc/postgresql/pg_hba.conf'` so the mounted
`pg_hba.conf` is actually read (without it PostgreSQL falls back to the copy in
`$PGDATA` and the mount is silently ignored). `hba_file` is startup-only, hence the
restart rather than a reload.

The init script is a template rendering `vault_joplin_postgres_password` from
`ansible/inventory/host_vars/eq12_docker/vault.yml`; it is written `999:999` mode
`0600` so the credential is not world-readable on the host, and is deployed with
`diff: false` so a `--diff` run never prints it.

`config/` is excluded from the deploy-dir rsync payload — the configs go straight to
their container mount paths, so a second copy under `/data/deploy/postgresql/` would
just be decorative drift.

### Standalone (non-Ansible) use

Place the config files yourself before `docker compose up`:

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

## Important Notes

- **PostgreSQL data**: UID 999 (postgres user in container)
- **pgAdmin data**: UID 5050 (pgadmin user in container)
- **Config files**: Read-only mounts, owned by root
- **Init scripts**: Executed only on first database initialization

Ensure adequate disk space as databases can grow significantly over time. The backup directory should also have sufficient space for automated backups.

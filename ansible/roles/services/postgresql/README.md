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

## Data Folder Permissions

The PostgreSQL container requires specific permissions for data directories. Set up the following directories with correct ownership:

```bash
# Create the data directories
sudo mkdir -p /data/postgresql/data
sudo mkdir -p /data/postgresql/config
sudo mkdir -p /data/postgresql/init-scripts
sudo mkdir -p /data/postgresql/pgadmin

# Set ownership to the PostgreSQL user (UID 999)
sudo chown -R 999:999 /data/postgresql/data

# Set ownership for pgAdmin (UID 5050)
sudo chown -R 5050:5050 /data/postgresql/pgadmin

# Config files can be owned by root (they're mounted read-only)
sudo chown -R root:root /data/postgresql/config
sudo chown -R root:root /data/postgresql/init-scripts

# Set appropriate permissions
sudo chmod -R 755 /data/postgresql/data
sudo chmod -R 755 /data/postgresql/pgadmin
sudo chmod -R 644 /data/postgresql/config/*
sudo chmod -R 644 /data/postgresql/init-scripts/*
```

## Important Notes

- **PostgreSQL data**: UID 999 (postgres user in container)
- **pgAdmin data**: UID 5050 (pgadmin user in container)
- **Config files**: Read-only mounts, can be owned by root
- **Init scripts**: Executed only on first database initialization

Ensure adequate disk space as databases can grow significantly over time. The backup directory should also have sufficient space for automated backups.

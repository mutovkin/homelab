# Joplin Server

## Description

[Joplin](https://joplinapp.org/) [Server](https://github.com/laurent22/joplin) is a synchronization service for the Joplin note-taking application. It provides a centralized server where Joplin clients can sync notes, notebooks, tags, and attachments. This server enables secure note synchronization across multiple devices while maintaining end-to-end encryption of note content.

Key features:

- Note synchronization across devices
- End-to-end encryption support
- Web clipper integration
- Email verification and user management
- Filesystem and database storage options
- SMTP configuration for notifications

## Data Folder Permissions

The `/data/joplin` folder must be accessible by the container's user (UID 1001). Set the correct permissions:

```bash
# Create the data directory
sudo mkdir -p /data/joplin

# Set ownership to the Joplin user (UID 1001)
sudo chown -R 1001:1001 /data/joplin

# Set appropriate permissions
sudo chmod -R 755 /data/joplin
```

The container uses filesystem storage for better performance, storing note data and attachments in the mounted volume. Ensure the directory is writable and has sufficient disk space for your notes and attachments.

## Pre-deploy database backup (#121a)

joplin-server is `monitor-only` (#83), so a new release only ever arrives through a
deliberate `task deploy:service -- --tags joplin`, whose `pull: always` hands the live
schema to a possibly newer image that runs **one-way migrations** on start. The role
therefore takes a `pg_dumpall` of the shared postgres cluster first, writes it to
`{{ data_mount }}/backups/joplin-pgdump-<ts>.sql`, and keeps `joplin_backup_retention`
of them.

**The gate is on the DATA, not on the container.** It fires when the joplin-server
container exists **OR** the `joplin` database exists in the cluster — the second half is
what covers a container-absent start after a `docker rm`, a `compose down`, or a failed
prior deploy, which is exactly the hole #107 fell through. Only the genuinely greenfield
case (no container *and* no database) skips, and it says so in a `debug` rather than
skipping silently. The database is probed with
`docker exec -u postgres postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='joplin'"`;
`-u postgres` is required — the cluster's local connections are `peer` authenticated and a
root invocation fails with `role "root" does not exist`. The verdict is stdout (`1` /
empty), never the exit code, which is 0 either way.

Dumps are written to `<name>.sql.partial` and renamed only after the tail check proves
`pg_dumpall` finished, so a truncated file can never be mistaken for a good backup. A
trap removes the partial on error and on the catchable termination signals; **SIGKILL
cannot be trapped**, so the role also sweeps stale `joplin-pgdump-*.sql.partial` files at
the start of every run — before taking the new dump, since a full volume is the likeliest
reason one was stranded, and the retention prune's `*.sql` glob does not match them.

## Configuration

Before starting the service:

1. Create environment file with database and SMTP settings
2. Ensure PostgreSQL is running and accessible
3. Configure the data directory permissions as shown above
4. The service will be available on port 22300

The server integrates with PostgreSQL for metadata storage while using the filesystem for efficient file storage.

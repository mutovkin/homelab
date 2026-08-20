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

## Pre-deploy database backup (#121a, narrowed in #142)

joplin-server is `monitor-only` (#83), so a new release only ever arrives through a
deliberate `task deploy:service -- --tags joplin`, whose `pull: always` hands the live
schema to a possibly newer image that runs **one-way migrations** on start. The role
therefore takes a dump first, writes it to
`{{ data_mount }}/backups/joplin-pgdump-<ts>.sql`, and keeps `joplin_backup_retention`
of them.

### What is in the dump

One file, two concatenated halves:

| Half | Command | Size (2026-08-19) |
| ---- | ------- | ----- |
| cluster globals — roles, tablespaces | `pg_dumpall --globals-only` | 946 B |
| the joplin database only | `pg_dump --create joplin` | ~633 MB |

It is **not** a `pg_dumpall`. The guard protects *joplin's* schema through a one-way
migration, so joplin's database is the right scope; the cluster-wide dump #121a started
with was incidental. Two design points that are not stylistic:

- **Globals first.** Joplin's objects are owned by role `joplin_user` and `pg_dump` never
  emits roles. Globals first means a restore into an empty cluster creates the role
  before anything grants to it.
- **`pg_dump --create`.** It emits `CREATE DATABASE joplin` + `\connect joplin`, so the
  file restores into the *right database* with a single `psql`. Without `--create`, the
  very same restore command creates joplin's tables inside the `postgres` database — a
  silent, plausible-looking wrong restore.

**This narrowing saved no disk, and the commit does not claim it did.** Measured before
the change: `joplin` 1369 MB of a cluster whose other databases (`template0`,
`template1`, `postgres`) total ~15 MB — 99.7% joplin. `pg_dump joplin` came out at
633 MB against `pg_dumpall`'s 632 MB. What it buys is scope (dump what the guard
protects), decoupling from cluster growth (the day a second service lands in this shared
postgres, a cluster dump would start dumping it on every routine joplin deploy), and
restore ergonomics.

**The file contains SCRAM password hashes** (in the globals half) and Joplin session
tokens. `umask 077` / mode `0600` on it is not cosmetic, and it must never be routed
through an Ansible register — that would copy it to the controller and print it in play
output.

Not in the dump: attachment blobs. `STORAGE_DRIVER` is `Filesystem`, so they live under
`{{ data_mount }}/joplin`. This guard is about a bad schema migration, not about data
loss on that volume.

### Restore procedure

The whole file restores in one command, into an empty or a rebuilt cluster:

```bash
docker exec -i -u postgres postgres psql -v ON_ERROR_STOP=1 -d postgres \
  < /data/backups/joplin-pgdump-<ts>.sql
```

`-u postgres` is required — local connections are `peer` authenticated (#79) and a root
invocation fails with `role "root" does not exist`. `-d postgres` is the *entry* database
only: the file's `\connect joplin` switches over after `CREATE DATABASE`. `ON_ERROR_STOP=1`
is what makes a failed restore fail instead of limping to the end with half a schema.

If the `joplin` database still exists, drop it first (or the `CREATE DATABASE` errors and
`ON_ERROR_STOP` aborts) — stop joplin-server before you do.

**Partial DR — cluster intact, joplin database lost — needs the pg_dump half ONLY.** This
is the likeliest real recovery scenario and the whole-file command above *fails* in it:
the globals half opens with `CREATE ROLE joplin_user`, the role already exists, and
`ON_ERROR_STOP=1` aborts **before any data lands**. Skip the globals and restore from the
pg_dump section header:

```bash
sed -n '/^-- PostgreSQL database dump$/,$p' /data/backups/joplin-pgdump-<ts>.sql \
  | docker exec -i -u postgres postgres psql -q -v ON_ERROR_STOP=1 -d postgres
```

That anchor — not `CREATE DATABASE` — is load-bearing; the next subsection explains why
slicing one line lower produces a database that exists and contains nothing. Use the
whole-file command only when the roles are genuinely absent (empty or rebuilt cluster).

#### Restoring under a scratch name (the periodic restore drill)

To prove the dump without touching the live database, restore the **pg_dump half** under
a different name. Slice from the pg_dump section header — **not** from `CREATE DATABASE` —
and rewrite only the three identity lines:

```bash
f=$(ls -t /data/backups/joplin-pgdump-*.sql | head -1)
sed -n '/^-- PostgreSQL database dump$/,$p' "$f" \
  | sed -e 's/^CREATE DATABASE joplin /CREATE DATABASE joplin_restore_test /' \
        -e 's/^\\connect joplin$/\\connect joplin_restore_test/' \
        -e 's/^ALTER DATABASE joplin /ALTER DATABASE joplin_restore_test /' \
  | docker exec -i -u postgres postgres psql -q -v ON_ERROR_STOP=1 -d postgres
```

It starts at the pg_dump header rather than at `CREATE DATABASE` because the globals'
`CREATE ROLE joplin_user` would collide with the role that already exists — and because of
a **PostgreSQL 18 trap that cost a false-negative restore drill when #142 landed**:

PG18 wraps each dump section in `\restrict <token>` / `\unrestrict <token>` (psql refuses
backslash meta-commands in between, so hostile dump content cannot inject them). With
`--create` the pairing is *nested*, not one span — measured on a real dump:

```
   5: \restrict          <- globals section opens
  32: \unrestrict        <- globals section closes
  39: -- PostgreSQL database dump      <- slice here
  42: \restrict          <- pg_dump section opens
  63: CREATE DATABASE joplin ...
  68: \unrestrict        <- pg_dump drops out of restricted mode ...
  69: \connect joplin    <- ... solely so THIS meta-command is allowed ...
  70: \restrict          <- ... then goes straight back in
9894300: \unrestrict     <- pg_dump section closes
```

Slicing at `CREATE DATABASE` (line 63) keeps the `\unrestrict` on line 68 but drops its
`\restrict` on line 42. psql then fails with `\unrestrict: not currently in restricted
mode` — **at line 68, after `CREATE DATABASE` has already succeeded.** The result is a
scratch database that exists, looks plausible, and contains **zero tables**. Only
`ON_ERROR_STOP=1` turns that into a nonzero exit (3) instead of a silent green restore
drill. Never run a restore drill without it, and never judge one by "the database is
there".

Verify the restore against the live database, then drop it — a leftover scratch copy is
~1.3 GB:

```bash
for db in joplin joplin_restore_test; do
  docker exec -u postgres postgres psql -d "$db" -tAc \
    "select '$db', (select count(*) from information_schema.tables where table_schema='public'),
            (select count(*) from pg_indexes where schemaname='public'),
            (select count(*) from items), (select count(*) from users)"
done
docker exec -u postgres postgres psql -d postgres -c "DROP DATABASE joplin_restore_test"
```

Table/index/row counts must match. `pg_database_size` legitimately does **not** — a fresh
restore has no dead tuples (1296 MB restored vs 1369 MB live when #142 landed).

### The gate is on the DATA, not on the container

It fires when the joplin-server container exists **OR** the `joplin` database exists in
the cluster — the second half is what covers a container-absent start after a
`docker rm`, a `compose down`, or a failed prior deploy, which is exactly the hole #107
fell through. Only the genuinely greenfield case (no container *and* no database) skips,
and it says so in a `debug` rather than skipping silently. The database is probed with
`docker exec -u postgres postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='joplin'"`;
the verdict is stdout (`1` / empty), never the exit code, which is 0 either way.

An "upgrade-pending" gate (vaultwarden's shape) is **rejected**, permanently: vaultwarden
stops its container to copy SQLite, so gating avoids downtime; this dump is online, so
gating buys only disk — while a wrong gate computation means *no backup* in front of a
one-way migration. Fail-safe over-backup beats fail-open under-backup here. This is also
why the dump is taken on **every** deploy and the second run of an idempotency check
legitimately reports `changed` on this task.

### Completion is verified with TWO markers, in bounded regions

The halves end with different strings, and one is a substring shape of the other:

| Half | Marker | Checked in |
| ---- | ------ | ---------- |
| globals | `-- PostgreSQL database cluster dump complete` | first 64 KB |
| joplin db | `-- PostgreSQL database dump complete` | last 20 lines |

A `pg_dumpall` file contains **both**, so the plain marker alone cannot tell a narrowed
dump from a cluster dump — check the exact string you mean. Neither check may run over
the whole file: this is a *notes* database, and a note whose text contains a marker would
satisfy an unbounded grep.

Two traps worth knowing before touching those five lines:

- **PostgreSQL 18 writes `\unrestrict <token>` AFTER the marker**, so the marker is not
  the last line of the file. A bounded `tail` region catches it; a "last line must be the
  marker" check would not. Do not write one.
- **No pipelines.** The checks are command substitution + herestrings, deliberately.
  Under `set -o pipefail`, `head … | grep -q` can fail *spuriously*: `grep -q` exits at
  its first match and SIGPIPEs the writer, the writer dies 141, `pipefail` propagates it,
  the ERR trap deletes a perfectly good dump and the `rescue:` refuses the deploy. The
  original `tail -n 20 | grep -q` escaped that only because 20 lines fit the 64 KB pipe
  buffer — luck, not design. Do not "simplify" them back.

Dumps are written to `<name>.sql.partial` and renamed only after both checks pass, so a
truncated file can never be mistaken for a good backup. A trap removes the partial on
error and on the catchable termination signals; **SIGKILL cannot be trapped**, so the
role also sweeps stale `joplin-pgdump-*.sql.partial` files at the start of every run —
before taking the new dump, since a full volume is the likeliest reason one was stranded,
and the retention prune's `*.sql` glob does not match them.

The retention prune globs exactly `joplin-pgdump-*.sql`. That is deliberately narrow:
`/data/backups` also holds ad-hoc operator dumps and another role's archives, and this
prune must never touch them.

## Configuration

Before starting the service:

1. Create environment file with database and SMTP settings
2. Ensure PostgreSQL is running and accessible
3. Configure the data directory permissions as shown above
4. The service will be available on port 22300

The server integrates with PostgreSQL for metadata storage while using the filesystem for efficient file storage.

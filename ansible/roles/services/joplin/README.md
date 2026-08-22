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

## Pre-deploy database backup (#121a, narrowed in #142, compressed in #147)

joplin-server is `monitor-only` (#83), so a new release only ever arrives through a
deliberate `task deploy:service -- --tags joplin`, whose `pull: always` hands the live
schema to a possibly newer image that runs **one-way migrations** on start. The role
therefore takes a dump first, writes it to
`{{ data_mount }}/backups/joplin-pgdump-<ts>.sql.gz`, and keeps `joplin_backup_retention`
of them.

Since #147 the artifact is **gzip-compressed** — `joplin-pgdump-<ts>.sql.gz`, a single
gzip member over the same two-half plain stream as before. Every restore recipe below is
therefore fronted by `zcat`. Pre-#147 artifacts are plain `joplin-pgdump-<ts>.sql`; for
those, drop the `zcat` and read the file directly (`sed -n … "$f"`, or `< "$f"`). Both
generations sit in one retention slice, so plain dumps age out on their own — do not
delete them by hand.

### What is in the dump

One file, two concatenated halves:

| Half | Command | Size (2026-08-19) |
| ---- | ------- | ----- |
| cluster globals — roles, tablespaces | `pg_dumpall --globals-only` | 946 B |
| the joplin database only | `pg_dump --create joplin` | ~633 MB |

…and since #147 the concatenation is `gzip -1`-compressed into one gzip member; the
sizes in this section are the PLAIN stream. See "Compression (#147)".

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

**This narrowing is size-neutral, and the commit does not claim otherwise.** Measured
2026-08-19 on artifacts taken 32 seconds apart on the same host:

| Artifact | Bytes |
| -------- | ----- |
| `pg_dumpall` (the old behaviour), 20:17:55 | 633,247,291 |
| globals + `pg_dump --create joplin` (this role), 20:18:27 | 633,245,733 |
| difference | **1,558 bytes smaller** |

~2 KB is the entire achievable saving, and always was. The other databases look
substantial by `pg_database_size` — `postgres` 7702 kB, `template0` 7678 kB, `template1`
7750 kB, ~22.6 MiB together — but that is **catalog and index storage, which `pg_dump`
never emits**. Their dumpable content is `pg_dump template1` = 720 B and
`pg_dump postgres` = 1,443 B; `template0` has `datallowconn = false` and is skipped
entirely. 2,163 B of dumpable content in, ~2 KB of saving out.

**Compare the two sizes at the same instant, or you will measure the wrong thing.**
Joplin is a live sync server: two consecutive dumps from this role, 230 s apart, differ by
3,968 B — about 62 KB/h. Over a 20.5 h gap that is ~1.27 MB, which dwarfs the ~2 KB the
format change is worth. Comparing a dump taken today against one from yesterday and
attributing the delta to the change is exactly how you would "prove" a regression that
does not exist. (Measured: predicted growth over that gap 1,272,003 B vs actual difference
1,273,206 B — the growth accounts for 99.9% of it, leaving nothing for the format change.)

What the change actually buys is scope (dump what the guard protects), decoupling from
cluster growth (the day a second service lands in this shared postgres, a cluster dump
would start dumping it on every routine joplin deploy), and restore ergonomics.

### Compression (#147)

Narrowing was size-neutral; **compression is the size lever**, because the cluster's
*dumpable* content is essentially all joplin — the other databases dump to ~2 KB in total
(measured just above), so the ~633 MB was never cluster overhead, it was joplin:

| Artifact | Bytes (2026-08-19, same window) |
| -------- | ----- |
| plain, both halves concatenated | 633,231,225 |
| after `gzip -1` | 309,412,797 |
| ratio | **~2.05x**, ~13 s of CPU on a ~9 s dump |

Only ~2x because the payload is largely high-entropy note content — raising the gzip
level buys little for much more CPU. Retention 7 therefore costs ~2.2 GB rather than
~4.4 GB. (Compare compressed against uncompressed **for the same artifact** —
`zcat "$f" | wc -c` vs `stat -c %s "$f"`. Never against a dump from another hour; see the
growth warning below.)

**The order is verify-then-compress, and that is the whole design.** The dump lands as a
plain `.partial`; both bounded completion markers are checked on that plain intermediate
in their herestring form; only then does `gzip -1` run, `gzip -t` verify the result, the
plain intermediate get removed, and the artifact get renamed. Nothing in the verification
path became a pipeline, so the SIGPIPE hazard described further down cannot occur. And
`gzip -t` is not a second-class check: gzip stores a CRC32 and the length of the
uncompressed stream, so a passing test proves the `.gz` decompresses to exactly the bytes
the markers verified.

Two alternatives were rejected. `-Fc` custom format needs `pg_restore`, kills the textual
completion-marker asserts, and invalidates every recipe on this page.
`pg_dump --compress=gzip:1` compresses only the pg_dump half, leaving the globals half
plain — a mixed plain+gzip file that no single tool decompresses.

**The file contains SCRAM password hashes** (in the globals half) and Joplin session
tokens. `umask 077` / mode `0600` on it is not cosmetic, and it must never be routed
through an Ansible register — that would copy it to the controller and print it in play
output.

Not in the dump: attachment blobs. `STORAGE_DRIVER` is `Filesystem`, so they live under
`{{ data_mount }}/joplin`. This guard is about a bad schema migration, not about data
loss on that volume.

### Restore procedure

**`gzip -t` first, in every recipe below.** A `zcat …| psql` pipeline's exit status is
*psql's*, so a missing, empty, or truncated input can leave you looking at exit 0 with
nothing restored — the same silent-green shape the PG18 slicing trap produced. `gzip -t`
re-checks the artifact's own shipped guarantee (the CRC32 and length gzip stored over the
uncompressed stream) at the moment it matters, and the `&&` stops the restore if it
fails. On a pre-#147 plain `.sql` there is no `gzip -t` to run — that generation ships no
integrity check of its own, which is one more reason to let them age out.
(`|` binds tighter than `&&`, so the `&&` gates the *whole* downstream pipeline, not
just the `zcat`.)

The whole file restores in one command, into an empty or a rebuilt cluster:

```bash
gzip -t /data/backups/joplin-pgdump-<ts>.sql.gz \
  && zcat /data/backups/joplin-pgdump-<ts>.sql.gz \
  | docker exec -i -u postgres postgres psql -v ON_ERROR_STOP=1 -d postgres
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
gzip -t /data/backups/joplin-pgdump-<ts>.sql.gz \
  && zcat /data/backups/joplin-pgdump-<ts>.sql.gz \
  | sed -n '/^-- PostgreSQL database dump$/,$p' \
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
f=$(ls -t /data/backups/joplin-pgdump-*.sql.gz | head -1)
gzip -t "$f" && zcat "$f" | sed -n '/^-- PostgreSQL database dump$/,$p' \
  | sed -e 's/^CREATE DATABASE joplin /CREATE DATABASE joplin_restore_test /' \
        -e 's/^\\connect joplin$/\\connect joplin_restore_test/' \
        -e 's/^ALTER DATABASE joplin /ALTER DATABASE joplin_restore_test /' \
  | docker exec -i -u postgres postgres psql -q -v ON_ERROR_STOP=1 -d postgres
```

On the success path every consumer here reads its input to EOF (`psql`,
`sed -n '/…/,$p'`), so nothing SIGPIPEs `zcat`. **That is only true on the success
path**: `psql -v ON_ERROR_STOP=1` exits at the FIRST error and then SIGPIPEs its
upstream, so "psql reads to EOF" is not a fact to carry into a `pipefail` script.
Likewise `ls -t … | head -1` *is* an early-exiting consumer — fine in an interactive
recipe with no `set -o pipefail`, and **not** to be carried into the role's shell task,
which runs under `pipefail`. See the marker section below for what that costs.

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

#### Order of operations, partials, and retention (#147)

The dump is written to `joplin-pgdump-<ts>.sql.partial` — **plain**. Both marker checks
run against that plain intermediate. Only then:

```
markers OK  →  gzip -1 -c  →  <name>.sql.gz.partial
            →  gzip -t     →  rm the plain intermediate
            →  mv          →  joplin-pgdump-<ts>.sql.gz
```

Verifying before compressing is what keeps the marker checks pipeline-free (two
`grep -q … <<<` herestrings among the four check lines, unchanged since #142) — a
`zcat "$f" | tail -n 20 | grep -q …` check would reintroduce exactly the SIGPIPE failure
described above. `gzip -t` then carries the guarantee across: it validates gzip's stored
CRC32 and length of the *uncompressed* stream, so it proves the `.gz` decompresses to the
bytes the markers already vouched for.

A killed run can therefore strand **two** shapes of partial, and both are covered:

| Shape | Left by | Cleaned by |
| ----- | ------- | ---------- |
| `joplin-pgdump-<ts>.sql.partial` | dump or marker check died | trap, and the pre-dump sweep |
| `joplin-pgdump-<ts>.sql.gz.partial` | gzip or `gzip -t` died | trap, and the pre-dump sweep |

The trap covers ERR and the catchable termination signals and names both files;
**SIGKILL cannot be trapped**, so the role also sweeps stale partials at the start of
every run — before taking the new dump, since a full volume is the likeliest reason one
was stranded. The sweep needs *both* patterns because `find`'s fnmatch is a **full**
match: `joplin-pgdump-*.sql.partial` does **not** match `x.sql.gz.partial`.

The retention prune globs `joplin-pgdump-*.sql` **and** `joplin-pgdump-*.sql.gz` — same
fnmatch reason (`*.sql` does not match `x.sql.gz`) — and sorts the combined list by
mtime, so the two generations share ONE slice of `joplin_backup_retention`. The pre-#147
plain ~633 MB dumps therefore age out on their own as compressed ones accumulate; do not
delete them by hand. Neither pattern matches a `.partial`, which is why the sweep exists
at all.

Both globs keep the `joplin-pgdump-` prefix, and that is deliberately narrow:
`/data/backups` also holds ad-hoc operator dumps (`pre-*.sql`, `pg_dumpall-post-*.sql`)
and vaultwarden's `.tgz` archives, and this prune must never touch them.

## Configuration

Before starting the service:

1. Create environment file with database and SMTP settings
2. Ensure PostgreSQL is running and accessible
3. Configure the data directory permissions as shown above
4. The service will be available on port 22300

The server integrates with PostgreSQL for metadata storage while using the filesystem for efficient file storage.

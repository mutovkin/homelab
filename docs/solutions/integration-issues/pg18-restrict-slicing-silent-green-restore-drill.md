---
title: "The restore drill that restored nothing: PG18 restrict-nesting broke the slice, and psql exits 0 on a half-restore"
date: 2026-08-19
category: integration-issues
module: services/joplin
problem_type: integration_issue
component: tooling
symptoms:
  - "psql aborts a restore with 'unrestrict: not currently in restricted mode' on a dump file that is byte-for-byte intact"
  - "The scratch database EXISTS after the failed restore, is owned by the right role, and contains zero tables"
  - "The abort lands AFTER CREATE DATABASE succeeded, so every identity-level check (backslash-l, SELECT datname FROM pg_database) says the restore worked"
  - "Without ON_ERROR_STOP=1 the same restore exits 0 and the drill reports success"
  - "A dump-slicing recipe that worked before a PostgreSQL major-version upgrade stops working after it, with no change on our side"
root_cause: wrong_api
resolution_type: config_change
severity: high
related_components:
  - postgresql
  - joplin
  - backups
  - ansible
tags:
  - postgresql
  - pg18
  - pg-dump
  - backup
  - restore-drill
  - on-error-stop
  - silent-failure
  - verification
---

# The restore drill that restored nothing: PG18 restrict-nesting broke the slice, and psql exits 0 on a half-restore

## Problem

#142 narrowed the Joplin pre-deploy dump from a whole-cluster `pg_dumpall` to
`pg_dumpall --globals-only` + `pg_dump --create joplin`. The new artifact was then verified
the only way a backup can honestly be verified — by restoring it. The drill sliced the
dump from its `CREATE DATABASE` line to EOF and rewrote the three database-identity lines
so the data would land in a scratch database rather than over the live one.

That recipe was correct against pre-18 `pg_dump` output and is **wrong** against
PostgreSQL 18. It produced a scratch database that existed, was owned by the right role,
looked entirely plausible — and contained **zero tables**.

## Symptoms

- `psql: error: … \unrestrict: not currently in restricted mode`, on an intact dump file.
- Exit code 3 — `ON_ERROR_STOP` firing. That is the *only* reason the failure was visible.
- `SELECT datname FROM pg_database` lists the restored database. `\l` shows it. Its owner
  is right. `SELECT count(*) FROM information_schema.tables WHERE table_schema='public'`
  returns **0**.

## What Didn't Work

- **Slicing the dump at `CREATE DATABASE`.** The obvious anchor, and the one the plan for
  #142 specified. It is precisely the anchor that breaks the file.
- **Judging the drill by "the database is there."** Every identity-level check passes on a
  restore that copied nothing, because `CREATE DATABASE` runs *before* the statement that
  aborts. "The artifact exists" is a guaranteed-true observation here and carries zero
  information.
- **Trusting psql's exit code by default.** psql's default is to report each error and
  keep going, exiting 0. A harness that shells out to psql and checks `$?` is, by default,
  checking nothing. Verified 2026-08-19: with `ON_ERROR_STOP=1` this run exited 3; without
  it, the same stream would have ended at EOF with status 0 and a green drill.

## Solution

PostgreSQL 18 wraps each dump section in `\restrict <token>` / `\unrestrict <token>`.
Between the pair psql refuses backslash meta-commands, so content inside a dump cannot
inject one. With `--create` the pairing is **nested, not one span**. Measured on the real
633 MB artifact:

```
      5: \restrict      <- globals section opens
     32: \unrestrict    <- globals section closes
     39: -- PostgreSQL database dump      <- the correct slice anchor
     42: \restrict      <- pg_dump section opens
     63: CREATE DATABASE joplin ...       <- the WRONG slice anchor
     68: \unrestrict    <- pg_dump leaves restricted mode ...
     69: \connect joplin      <- ... solely so THIS meta-command is permitted ...
     70: \restrict            <- ... then re-enters immediately
9894300: \unrestrict    <- pg_dump section closes
```

Slicing at line 63 keeps the `\unrestrict` on line 68 and drops its `\restrict` on line
42. psql fails at 68 — after `CREATE DATABASE` on 63 has already committed.

Slice from the pg_dump **section header** instead, so the pair stays balanced.

**Historical (pre-#147)** — the recipe exactly as it stood on 2026-08-19, when the
artifacts were plain `.sql`. Kept as the dated record; **do not run it now**, because the
`*.sql` glob silently selects a stale plain dump while both generations coexist in
retention, and matches nothing at all once they age out:

```bash
# ansible/roles/services/joplin/README.md — "Restoring under a scratch name"
f=$(ls -t /data/backups/joplin-pgdump-*.sql | head -1)
grep -c '^-- PostgreSQL database dump$' "$f"     # assert the anchor is unique: must be 1

sed -n '/^-- PostgreSQL database dump$/,$p' "$f" \
  | sed -e 's/^CREATE DATABASE joplin /CREATE DATABASE joplin_restore_test /' \
        -e 's/^\\connect joplin$/\\connect joplin_restore_test/' \
        -e 's/^ALTER DATABASE joplin /ALTER DATABASE joplin_restore_test /' \
  | docker exec -i -u postgres postgres psql -q -v ON_ERROR_STOP=1 -d postgres
```

**Current form (#147)** — artifacts are now `joplin-pgdump-<ts>.sql.gz`. The slicing
lesson below is unchanged and format-independent; only the front of the pipeline moved.
The live recipe of record is
[ansible/roles/services/joplin/README.md](../../../ansible/roles/services/joplin/README.md):

```bash
f=$(ls -t /data/backups/joplin-pgdump-*.sql.gz | head -1)
gzip -t "$f"                                     # the artifact's own CRC32, checked first
zcat "$f" | grep -c '^-- PostgreSQL database dump$'   # anchor must be unique: 1

gzip -t "$f" && zcat "$f" | sed -n '/^-- PostgreSQL database dump$/,$p' \
  | sed -e 's/^CREATE DATABASE joplin /CREATE DATABASE joplin_restore_test /' \
        -e 's/^\\connect joplin$/\\connect joplin_restore_test/' \
        -e 's/^ALTER DATABASE joplin /ALTER DATABASE joplin_restore_test /' \
  | docker exec -i -u postgres postgres psql -q -v ON_ERROR_STOP=1 -d postgres
```

The anchor-uniqueness assert becomes a pipeline (`zcat "$f" | grep -c`) because the file
is no longer readable in place. That is safe: `grep -c` counts every match and therefore
reads to EOF, so it cannot SIGPIPE `zcat` — unlike `grep -q`, which exits at the first
match and is exactly what must never appear downstream of a producer under `pipefail`.
And `gzip -t` goes first because the exit status of a `zcat … | psql` pipeline is
*psql's*: a missing or truncated input can otherwise present as a green exit 0 with
nothing restored — the same silent-green shape this whole document is about.

Then judge it by **counting objects on both sides**, never by the database existing:

```bash
for db in joplin joplin_restore_test; do
  docker exec -u postgres postgres psql -d "$db" -tAc \
    "select '$db',
       (select count(*) from information_schema.tables where table_schema='public'),
       (select count(*) from pg_indexes where schemaname='public'),
       (select count(*) from items), (select count(*) from users)"
done
docker exec -u postgres postgres psql -d postgres -c "DROP DATABASE joplin_restore_test"
```

Verified 2026-08-19: rc=0, and 26/26 tables, 79/79 indexes, 1128/1128 items, 2/2 users,
owner `joplin_user` on both sides. `pg_database_size` legitimately differs (1369 MB live
vs 1296 MB restored — a fresh restore has no dead tuples), so size is not a comparison
key. `-u postgres` is required throughout: local connections are `peer` authenticated
(#79) and a root invocation fails outright.

The working recipe is now written down beside the thing it verifies, in
`ansible/roles/services/joplin/README.md`.

## Why This Works

Three failures were stacked, and only the last one was visible:

1. **The slice broke a lexical pair.** `\restrict`/`\unrestrict` is *state on the psql
   session*, not decoration in the file. Any edit that cuts a dump at an arbitrary line
   can break it, and `--create` makes the nesting genuinely non-obvious: pg_dump exits
   restricted mode mid-section purely so it is allowed to run one `\connect`, then
   re-enters on the next line.
2. **The abort landed after a side effect.** Any restore whose first statements create the
   container for the data leaves that container behind when it fails — so the cheapest
   check is also the one that can never fail.
3. **psql's default is to keep going.** `ON_ERROR_STOP=1` is the control that converts a
   partial restore into a loud failure, and it is off by default.

The underlying class: **a verification recipe is code, and it rots against the versions it
verifies.** This one was written against pre-18 `pg_dump` output and stopped being a
verification the moment the server moved to 18 — the same way a config file rots, except
that a rotted *verification* removes the very signal that would have told you. It is the
repo's silent-green shape (see the Related section) turned on the guard itself: the ritual
kept running and kept reporting success while proving nothing.

## Prevention

- **`-v ON_ERROR_STOP=1` on every psql invocation that restores or migrates.** Not
  stylistic. Without it the exit status reports "we reached EOF", not "the restore
  worked" — the same shape as `set -e` on a shell task that echoes a marker.
- **Never judge a restore by identity.** Count objects — tables, indexes, and at least one
  row count from a real table — and compare against the source. Explicitly ignore
  `pg_database_size`.
- **Two measurements taken at different times do not measure your change.** The same trap
  that makes `pg_database_size` a bad comparison key (dead tuples) bites harder across
  time: this is a live sync server growing ~62 KB/h, so two dumps 20 h apart differ by
  ~1.27 MB — orders of magnitude more than the ~2 KB the #142 format change was worth.
  Comparing them and attributing the delta to the change "proves" a regression that does
  not exist. Compare artifacts captured seconds apart, and predict the drift from a
  measured rate before believing any residue. Related: a database's `pg_database_size` is
  mostly catalog and index storage that `pg_dump` never emits — `template1` is 7750 kB on
  disk and 720 bytes dumped — so sizing a dump from `pg_database_size` is a category
  error.
- **Assert your slice anchor is unique before slicing** (`grep -c '^…$'` must be 1). A
  regexp that matches a second, unintended line is the same defect wearing a different
  hat.
- **Re-run the restore drill after any PostgreSQL major-version upgrade**, not only after
  a change to the backup code. The upgrade is what invalidates the recipe, and nothing
  about the upgrade will tell you.
- **A backup is not verified until a restore is verified.** A dump nobody has restored is
  a hypothesis. #142 required this drill for exactly that reason, and the drill promptly
  found that the drill was broken.

### Adjacent traps from the same five lines of backup code

Both are documented at length in `ansible/roles/services/joplin/README.md` and in
`ansible/roles/services/joplin/tasks/main.yml`:

- **The completion markers differ, and one is a substring shape of the other.**
  `pg_dumpall` ends `-- PostgreSQL database cluster dump complete`; `pg_dump` ends
  `-- PostgreSQL database dump complete`. A `pg_dumpall` file contains **both**, so a
  check for the plain marker passes on a cluster dump too and cannot tell the two apart.
  Grep the exact string you mean, bounded to the region it belongs in (head 64 KB / tail
  20 lines) — never over the whole file, because this is a *notes* database and a row's
  text could be the marker. PostgreSQL 18 also writes `\unrestrict <token>` **after** the
  marker, so "the marker must be the last line" is a wrong check.
- **`head … | grep -q` can fail spuriously under `set -o pipefail`.** `grep -q` exits at
  its first match and SIGPIPEs the writer, the writer dies 141, `pipefail` propagates it,
  and the `ERR` trap then deletes a perfectly good backup. The pre-existing
  `tail -n 20 | grep -q` escaped this only because 20 lines fit the 64 KB pipe buffer —
  luck, not design. Use command substitution + herestrings, which contain no pipeline.
  This is a second, non-arithmetic way for a run to destroy the backup it just took, next
  to the retention-slice hazard already recorded in the conventions doc below.

## Related Issues

- #142 — narrowed the Joplin pre-deploy dump; this drill is its acceptance criterion
- #121 — added the pre-deploy dump guard the drill is verifying
- #107 — the pre-upgrade backup silently skipped when the container was absent but data existed
- #79 — peer authentication, which is why every command here runs `-u postgres`
- [[lxc-features-nfs-invalid-key-silent-green]] — the closest sibling: a shell task that
  swallowed a nonzero exit and echoed a marker reporting *intent, not outcome*, plus the
  section-unaware grep. `ON_ERROR_STOP=1` is `set -e` for psql.
- [[postgresql-mounted-configs-never-deployed-or-read]] — same cluster and module; "ask
  the process what it read, don't trust the container-level fact" is the same epistemics
  as "count the objects, don't trust that the database exists"
- [[vector-057-silent-log-pipeline-failure]] — #73, the first recorded instance of the
  silent-green class; "assert the data arrived, not that the process is running"
- [[create-time-only-fields-are-rebuild-declarations]] — #127 leaves the restore-before-deploy
  step as operator discipline with no enforced gate; this doc supplies the missing
  "and prove the restore actually restored something" half
- [[ansible-change-loop-pitfalls]] — §6 "a skipped branch is an untested branch" and §8's
  destructive-retention arithmetic. Note this dump runs on *every* deploy, so there was no
  branch to force — running the code was never the missing evidence; restoring its output
  was.
- [[unattended-upgrades-silently-inert-fleet-wide]] — an artifact that exists, looks
  correct, and is consumed by nobody

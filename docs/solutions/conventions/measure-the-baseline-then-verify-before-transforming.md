---
title: "The narrowing that saved nothing: measure the baseline your justification rests on, then verify before you transform"
date: 2026-08-21
category: conventions
module: services/joplin
problem_type: convention
component: tooling
severity: high
applies_when:
  - "justifying a change by a claimed size, cost, or performance saving before that change ships"
  - "reading an on-disk size (pg_database_size, du) as a proxy for how large the corresponding dump will be"
  - "adding compression or any transform to an artifact that already has content-level verification"
  - "editing a shell task that runs under set -o pipefail with an ERR trap that deletes its own output"
  - "claiming a documented runbook command is byte-for-byte the command that was executed live"
related_components:
  - postgresql
  - joplin
  - backups
  - ansible
tags:
  - backup
  - compression
  - verification
  - measurement
  - pipefail
  - sigpipe
  - restore-drill
  - pg-dump
---

# The narrowing that saved nothing: measure the baseline your justification rests on, then verify before you transform

## Context

#142 narrowed the Joplin pre-deploy backup from a whole-cluster `pg_dumpall` to
`pg_dumpall --globals-only` + `pg_dump --create joplin`, and was filed as a size fix: the
role dumped the entire shared cluster on every deploy at ~632 MB a time, and seven
retained came to ~4.4 GB of steady state.

The change was right, and it reclaimed **1,558 bytes**.

It was right on *scope* grounds — the guard protects joplin's schema through a one-way
migration, so joplin's database is the correct thing to dump, and it decouples the artifact
from cluster growth the day a second service lands in that shared postgres. Those arguments
stand on their own and needed no size claim at all. But the cost model was measured against
`pg_database_size`, which reports catalog, index, and free-space storage that `pg_dump`
never emits. The "shared cluster" was ~all joplin. The 4.4 GB steady state the change was
sold as reducing was still there afterwards.

#147 is the change that actually moves it — `gzip -1`, ~2x — and getting there surfaced two
further disciplines. Compressing this particular artifact the obvious way would have dragged
its completion check into a pipeline that, under `set -o pipefail`, deletes a perfectly good
backup and refuses the deploy. And the restore runbook the compression invalidated had to be
re-proved in a way that a side-by-side reading of two shell strings cannot achieve.

Six rules came out of one change. Three of them failed together, which is why they are
one page: a justification measured against the wrong quantity, a verification that a
transform would have quietly demolished, and a runbook whose correctness nobody could
actually see. The other three are the fan-out, integrity-gating, and
record-the-refusal disciplines those three dragged in behind them.

## Guidance

### 1. Measure the baseline your justification rests on — at the same instant

A claim like "this reduces backup size" is a claim about bytes on disk. It needs two byte
counts, not a plausible mechanism. #142's mechanism was sound (dump less) and its saving was
1,558 bytes, because the quantity it reasoned about is not the quantity it promised to
reduce.

The arithmetic, once the right baseline is used, is trivial and inverts the conclusion:

| Database | `pg_database_size` | What `pg_dump` emits |
| -------- | ------------------ | -------------------- |
| `template1` | 7750 kB | **720 B** |
| `postgres` | 7702 kB | **1,443 B** |
| `template0` | (see note) | skipped — `datallowconn = false` |

Four orders of magnitude apart. The entire non-joplin dumpable content of the cluster is
~2 KB, so scope was never the size lever — the 633 MB was never cluster overhead, it *was*
joplin — and the only remaining levers are compression and retention.

> Note, and it is on-topic: two records of `template0`'s size disagree — issue #147 says
> 7521 kB, the role README says 7678 kB. `postgres` and `template1` agree exactly in both.
> The figure is irrelevant to the conclusion (`template0` is never dumped), so neither is
> adopted here. A doc about measurement discipline does not get to silently pick one of two
> conflicting numbers.

**The same-instant half is a separate rule, and it bites independently of which baseline you
pick.** Joplin is a live sync server growing ~62 KB/h. Two dumps 20 h apart differ by
~1.27 MB — roughly six hundred times the ~2 KB a format change is worth. That is
enough to "prove" a regression that does not exist, or to hide a real one.

It nearly did. The first comparison run for #142 reported the narrowed dump as *larger*
(633,245,733 B vs 631,972,527 B) and that finding was relayed up the chain before anyone
re-derived it. The `pg_dumpall` number was from 20.5 hours earlier. Re-measured 32 seconds
apart on the same host: `pg_dumpall` 633,247,291 B vs narrowed 633,245,733 B — 1,558 bytes
*smaller*. Predicted growth across the original gap was 1,272,003 B against an actual
difference of 1,273,206 B: growth accounts for 99.9% of it, leaving nothing for the format
change (session history).

So: establish the drift rate first, predict the drift across your gap, and refuse to believe
any residue smaller than it. A comparison across time does not measure your change; it
measures the clock.

**And when the measurement kills the justification, keep the change and fix the record.**
The role comment now reads, in as many words, "Do NOT sell this as a size fix: it is
size-NEUTRAL", with both artifact sizes and the 32-second gap beside it. A size claim in a
commit message is a durable artifact — when it is retired, sweep the retired numbers rather
than merely superseding them, or the next reader spends a day hunting a regression that is
really an unbudgeted 4.4 GB.

### 2. Verify, then transform — and carry the verification across with the transform's own checksum

The Joplin dump's completion check is four of the most trap-laden lines in this repo: two
bounded textual markers, each written as a region capture plus a herestring `grep -q`,
deliberately rather than as pipelines. Under `set -o pipefail`, `head -c 65536 "$f" | grep -q '…'` can fail on a
*good* dump — `grep -q` exits at its first match, SIGPIPEs the writer, the writer dies 141,
`pipefail` propagates, the ERR trap removes the artifact, and the block's `rescue:` refuses
the deploy.

Adding compression the obvious way puts a `zcat` in front of exactly that shape.

The ordering that avoids it entirely:

```
dump → PLAIN <name>.sql.partial
     → both marker checks, on the plain intermediate, unchanged
     → gzip -1 -c  → <name>.sql.gz.partial
     → gzip -t
     → rm the plain intermediate
     → mv          → joplin-pgdump-<ts>.sql.gz
```

`gzip -t` is what makes this sound rather than merely convenient. Gzip's trailer stores a
CRC32 and the length of the *uncompressed* stream, so a passing test proves the `.gz`
decompresses to exactly the bytes the markers vouched for. The verification is not repeated
in a weaker form after the transform — it is **carried across** by an integrity primitive the
format itself provides.

Generalised: when you add compression, encryption, encoding, or upload to an artifact that
is already content-verified, run the check on the pre-transform bytes and carry it across
with the transform's own integrity primitive (`gzip -t` for gzip; a stored digest
otherwise). The tempting alternative — read the transformed artifact back through a
decompressor into the existing check — converts a pipeline-free check into a pipeline, which
is a *different* check with different failure modes.

Three orderings were rejected for the same single reason (they place `grep -q` downstream of
a producer under `pipefail`): compress-then-verify; streaming the dump straight into `gzip`
with no plain intermediate; and re-reading the `.gz` through `zcat` into the existing
asserts. Two format alternatives were rejected for other reasons worth recording — `-Fc`
custom format needs `pg_restore` and destroys both textual marker asserts along with every
restore recipe that treats the dump as text, and `pg_dump --compress=gzip:1` compresses only
the `pg_dump` half, leaving the `pg_dumpall --globals-only` half plain: a mixed plain+gzip
file no single tool decompresses.

**"It works today" proves nothing here.** The original form of this check (introduced with
the guard in #121a, replaced by #142 — before compression was ever on the table) used
`tail -n 20 | grep -q`, and survived only because 20 lines fit inside the 64 KB pipe buffer,
so `tail` finished writing before `grep` exited. Luck, not design. Put a 633 MB producer in front of the same consumer
and the failure becomes probabilistic, arrives during a real deploy, deletes the backup, and
blocks the upgrade.

Where a pipeline is genuinely required, choose a consumer that reads to EOF. `grep -c`
counts every match and is safe exactly where `grep -q` is not — which is why the
anchor-uniqueness assert in the drill recipe may be `zcat "$f" | grep -c '…'`.

### 3. Changing an artifact's name is a fan-out change — enumerate every consumer

`ansible.builtin.find`'s fnmatch is a **full** match. `*.sql` does not match `x.sql.gz`;
`*.sql.partial` does not match `x.sql.gz.partial`. Appending one suffix moves the artifact
out of every existing glob at once, and there is no partial-credit failure: the globs do not
"mostly work", they silently select nothing — or worse, select only the stale generation and
keep reporting green.

For this one file the consumers were: the ERR/EXIT trap, the pre-dump partial sweep, the
retention prune, the check-mode preview text, the role defaults comment, three README
restore recipes, and a drill recipe in a *different* `docs/solutions/` page.

Handled deliberately, the same property becomes the migration plan. Listing both patterns in
the retention `find` puts both generations into ONE mtime-sorted slice, so the old plain
dumps age out on their own as compressed ones accumulate — no hand-deletion, no second
retention counter. Keeping the shared `joplin-pgdump-` prefix on all four patterns is what
keeps the prune off the ad-hoc operator dumps and another role's archives in the same
directory.

### 4. Prove a documented runbook command is the one you executed — by hashing bytes, not by reading

A restore runbook is not documentation *about* the deliverable; it **is** the deliverable —
what someone pastes at 3 a.m. with the database gone. So test it the way it will be used:
verbatim, composed, end to end.

Reading two shell strings side by side cannot do this. Between a committed README and what a
shell actually parses sit `ssh`, `pct exec`, and a nested `bash -c`, any of which can
transform the text with no visible sign. A visual diff cannot see a quoting-level
difference; a hash can.

The method is to make re-parsing *impossible* rather than merely unlikely:

1. Extract the recipe bytes from the committed file (`git show <ref>:<path>`, sliced to the
   code block).
2. Transport them through something that cannot alter bytes — base64 — into a file on the
   target host.
3. Execute the **file** (`bash /tmp/drill.sh`), so no shell on the way re-parses the text.
4. Read the file back and compare hashes at both ends.

Measured for #147: 436 B, sha256
`767a62d05314beebdcf59f679822fa3834f9dd88e00af5d2b8468fbfd0aa0132`, identical on the
controller, on the container, and on the round-trip.

**Prove the composition, not just the halves.** That drill had already been run once without
the `gzip -t` front-end, and the front-end's precedence had been measured separately. Two
correct halves plus one plausible join is exactly how `&&`, pipelines, and SIGPIPE have
surprised this repo before. If the composed thing is what ships, the composed thing is what
must be executed.

### 5. Front every "decompress and restore" pipeline with the artifact's own integrity check

`zcat "$f" | … | psql` exits with **psql's** status. A missing, empty, or truncated artifact
can therefore present as exit 0 with nothing restored — the same silent-green shape
[pg18-restrict-slicing-silent-green-restore-drill](../integration-issues/pg18-restrict-slicing-silent-green-restore-drill.md)
documents from the other direction.

`gzip -t "$f" && …` re-checks the CRC32 and length the artifact ships with, at the moment it
matters. And `|` binds tighter than `&&`, so the gate covers the entire downstream pipeline
rather than just the `zcat` — a claim worth verifying once rather than asserting:

```bash
bash -c 'false && echo A | cat -'   # no output at all, exit 1
bash -c 'true && printf hello | cat -'   # hello, exit 0
```

The corollary for the older generation: plain `.sql` dumps ship no integrity check of their
own. One more reason to let them age out rather than keep them.

### 6. Record a refused optimisation, with the condition that would reverse it

Between `gzip -1` and the `rm`, the plain intermediate (~633 MB) and the compressed partial
(~310 MB) coexist, so a run peaks at ~940 MB of scratch against ~633 MB before. That is a
real regression, accepted, because the only obvious fix — streaming — is precisely what
reintroduces the pipeline hazard from rule 2.

Write that down at the site, including the condition under which the trade should be
revisited (here: `/data` no longer having 99 G free). Otherwise the next reader "simplifies"
it and reintroduces the defect with a clean conscience. The same applies to the `rm`-before-`mv`
ordering: both orders are safe, one was chosen (an `rm` failure fires ERR, the trap discards
the verified `.gz`, and the rescue refuses — fail-closed and loud), and the reason belongs in
the file rather than in someone's memory.

## Why This Matters

The first rule is the expensive one, because its failure is invisible. Nothing breaks. Tests
pass. The deploy is green. The change is even *correct*. What is wrong is the story attached
to it, and stories are what the next engineer plans against — a follow-up sized against a
saving that never happened, a capacity forecast built on 2.2 GB when the disk holds 4.4 GB,
a "regression" hunt for bytes that were always going to be there.

The second rule protects against a failure that is worse than invisible: it is *destructive
and intermittent*. A `zcat | grep -q` verification does not fail on the day you write it. It
fails during some later real deploy, at which point the ERR trap deletes the backup that was
about to protect a one-way schema migration, and the deploy refuses — meaning the operator's
next move is to work out how to get past the guard. That is the exact shape of #107 — the
failure class this whole backup mechanism exists to prevent.

The third, fourth and fifth exist because a runbook is used precisely when nobody has the
patience to debug it. Discovering during a recovery that the documented glob selects a stale
artifact (rule 3), that the command parses differently than it reads (rule 4), or that a
`zcat | psql` returned 0 without ever reading its input (rule 5), is discovering it at the
worst possible moment. The sixth is insurance against a later reader deleting one of the
other five for looking untidy.

## When to Apply

- Before writing any size, latency, or cost claim into an issue, commit message, or PR body
  — produce the measurement that would falsify it, or make the change on its real merits.
- Whenever an on-disk size is about to stand in for the size of something derived from it
  (`pg_database_size` vs a dump; `du` vs an archive; a table's storage vs its export).
- When comparing two measurements of anything that is still being written to.
- Before adding compression, encryption, encoding, or an upload step to an artifact that
  already has content-level verification.
- When editing any shell task that runs under `set -o pipefail` alongside a trap that removes
  its own output.
- When an artifact's name, extension, or location changes — enumerate every glob, trap,
  retention rule, and documented recipe that names it.
- Before claiming a documented command and an executed command are the same command.

## Examples

**The check that must not become a pipeline.** Its herestring form is unchanged across
#147 — the two `grep -q … <<<` lines are byte-identical, and the two region-capture lines
only re-point at the plain intermediate. This is the shape to preserve, in
`ansible/roles/services/joplin/tasks/main.yml`:

```bash
head_region="$(head -c 65536 "$plain")"
grep -q 'PostgreSQL database cluster dump complete' <<<"$head_region"
tail_region="$(tail -n 20 "$plain")"
grep -q 'PostgreSQL database dump complete' <<<"$tail_region"
```

Command substitution + herestring: no pipeline, so no SIGPIPE, so no `pipefail`
propagation, so no ERR trap on a healthy dump. The transform goes *after* these lines, never
around them.

**Both generations in one retention slice**, same file:

```yaml
    patterns:
      - "joplin-pgdump-*.sql"
      - "joplin-pgdump-*.sql.gz"
```

Live result across two applies: each pruned exactly one file — the oldest aging out beyond
the retention of 7 — leaving seven files, two `.sql.gz` and five `.sql`, in one mtime-sorted
list, with the ad-hoc operator dumps and another role's archives untouched.

**A same-artifact ratio measurement**, which is the only honest kind:

```bash
stat -c %s "$f"        # 310,920,468
zcat "$f" | wc -c      # 636,338,445  -> 2.047x
```

Never against a dump from another hour. The 2026-08-19 same-window pair that justified the
change: 633,231,225 B plain vs 309,412,797 B at `gzip -1`, ~2.05x, ~13 s of gzip CPU on a
~9 s dump. Only ~2x because the payload is largely high-entropy note content — raising the
level buys little for much more CPU.

**A doc-only follow-up round, proved without touching the live host:**

```bash
git diff HEAD~1 HEAD -- <task-file> | grep -E '^[-+]' \
  | grep -v '^[-+][-+][-+]' | grep -vcE '^[-+]\s*#'      # -> 0
```

Plus a comments-stripped comparison of the two revisions coming back identical. That is a
cheap, checkable reason not to re-apply — and the kind of evidence that keeps a "docs only"
claim from being a vibe.

**A comment that describes a diff must be checked against the diff.** The first version of
#147's compression comment claimed that the five herestring lines below stayed
byte-identical. Two of the four check lines are herestrings, and the two region-capture lines
*did* change (they read `$plain` now) — wrong in both count and scope. The same round replaced an unsourced
"~99.7% joplin" with a figure derivable from the numbers already on the page ("the other
databases dump to ~2 KB in total"). A repo that lectures about unsourced numbers does not get
to ship one.

## Related

- [pg18-restrict-slicing-silent-green-restore-drill](../integration-issues/pg18-restrict-slicing-silent-green-restore-drill.md)
  — the direct predecessor: same role, same block of backup code, same drill. (That page and
  the role README both say "five lines" where the file holds four check lines plus the
  redirect that produces them; the count here is of the four checks.) It owns
  the slice anchor, `ON_ERROR_STOP=1`, the two completion markers, and the `grep -q` SIGPIPE
  hazard. #147 changes only the artifact's *shape*, so that doc's drill recipe now carries a
  Historical (pre-#147) block alongside a current `.sql.gz` form.
- [A verification is only evidence if its instrument can tell the fixed state from the broken one](verification-instrument-must-distinguish-fixed-from-broken.md)
  — "do not inherit a measurement without re-deriving what it measured." #142's savings
  estimate is that error one level up: the number was inherited into a *justification* rather
  than into an acceptance bar.
- [Validation passing is not delivery](prove-notification-delivery-not-just-config-validity.md)
  — "a redactor in the pipeline eats the exit code" generalises to every `zcat … | psql`
  restore recipe here: the status you read is the last stage's.
- [ansible-change-loop-pitfalls](ansible-change-loop-pitfalls.md) — destructive retention
  arithmetic (the `joplin_backup_retention >= 1` assert that runs before anything is written
  comes from there). This doc adds the glob half of the same hazard.
- [unattended-upgrades-silently-inert-fleet-wide](../security-issues/unattended-upgrades-silently-inert-fleet-wide.md)
  — a pattern that is syntactically valid and matches nothing; a retention glob after a
  filename change is the backup-side instance.
- [lxc-features-nfs-invalid-key-silent-green](../integration-issues/lxc-features-nfs-invalid-key-silent-green.md)
  — a shell task that swallowed a nonzero exit and echoed a marker. The dump script is its
  inverse: `set -euo pipefail`, an ERR trap naming *both* intermediates, and a path printed
  only after the rename.

Issues: #147 (this change), #142 (the narrowing whose cost framing this corrects — still
correct on scope grounds), #121a (the pre-deploy dump guard), #107 (the pre-upgrade backup
that silently skipped, and the reason the gate is on the data), #83 (joplin-server's
`monitor-only` posture, which is why a new image only ever arrives through a deliberate
deploy), #79 (peer auth, which is why every `psql`/`pg_dump` here runs `-u postgres`), #158
(a check-mode ergonomics follow-up filed from #147's review).

This complements rather than repeats the backup bullet already in `CLAUDE.md`, which says a
backup is not verified until a restore is, and a restore is not verified by "the database is
there." The two halves it does not cover are here: **measure the baseline your justification
rests on, at the same instant**, and **verify before you transform, then carry the
verification across with the transform's own checksum**.

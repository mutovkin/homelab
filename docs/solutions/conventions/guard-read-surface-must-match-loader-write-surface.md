---
title: "A static guard is only as strong as the surface it reads and the harness that proves it"
date: 2026-08-29
category: conventions
module: observability
problem_type: convention
component: tooling
severity: high
applies_when:
  - "adding a control-machine guard that parses files a remote loader will later read"
  - "the deploy ships a whole DIRECTORY (rsync/synchronize) but the guard reads a narrower glob"
  - "building a fault-injection harness to prove a guard actually fails on the defect"
  - "an id in a config document is pinned and valid but is not a legal target for the thing linking to it"
  - "using ansible `fileglob` over anything deeper than one flat directory"
related_components:
  - services/observability
  - grafana
  - alerting-provisioning
  - ansible-lookup-plugins
tags:
  - ansible
  - grafana
  - guard-design
  - fault-injection
  - fileglob
  - filetree
  - provisioning
  - review-loop
---

# A static guard is only as strong as the surface it reads and the harness that proves it

## Context

#181 added the first repo-side gate for a failure this repo had already taken to the
face: a Grafana `__dashboardUid__` annotation with no `__panelId__` beside it is
boot-fatal for the **whole** Grafana process and withdraws every provisioned alert
rule ([grafana-alert-panelid-pairing-breaks-all-provisioned-rules](../integration-issues/grafana-alert-panelid-pairing-breaks-all-provisioned-rules.md),
measured 2026-08-24). The alerting YAML lives under
`ansible/roles/services/observability/files/data/grafana/provisioning/alerting/` and is
rsynced verbatim,
so the document stays valid YAML, `ansible-lint` and `--syntax-check` pass, and
`--check` skips the Grafana restart — the live apply was the first real test of
every deep-link edit ever made.

The gate itself was straightforward: parse the alert rules and the dashboard JSON on
the control machine, fail the deploy when a link is unpaired or points at nothing.
It passed on the tree, it failed on three hand-made defects, and it looked finished.

Then it was attacked. A reviewer built adversarial fixtures and ran a verbatim
replica of the guard: **it went green over inputs containing one instance of each
fault class its own comment enumerated** — four ways past it (a narrower read surface
than the deploy's write surface, duplicate dashboard uids collapsing in the
accumulator, row ids treated as linkable, and a pinning count that did not dedupe).
A fifth defect was self-inflicted, in the harness that was supposed to be proving the
fixes. A sixth had already been caught earlier in the same change by the guard's own
witness assert — a lookup that silently read zero files.

All of them are recorded below, because none is specific to Grafana: they are ways a
*static guard* fails while reporting success, which is this repo's most expensive
failure shape.

## Guidance

### 1. The guard's READ surface must equal the deploy's WRITE surface

The first version read `query('fileglob', dir ~ '/*.yaml')`. Two facts made that
narrower than reality:

* the role **rsyncs the whole directory** to the host, so anything in it ships;
* Grafana's provisioner loads `.yaml`, `.yml` **and** `.json` out of that directory.

An `evil.yml` carrying the exact boot-fatal half-pair was therefore never read, and
every witness assert passed happily on the other seven files. The guard reported
green over the precise defect it exists to prevent.

Two changes fix it, and the second is the one that keeps it fixed:

```yaml
# READ what the loader reads: an extension SET, walked recursively.
_obs_alerting_files: >-
  {{ query('filetree', _obs_alerting_dir_links)
     | selectattr('state', 'equalto', 'file')
     | map(attribute='src') | select('search', '\.(ya?ml|json)$') | sort }}

# ASSERT THE COMPLEMENT: the directory may hold nothing this guard skips.
_obs_alerting_unread: >-
  {{ query('filetree', _obs_alerting_dir_links)
     | selectattr('state', 'equalto', 'file')
     | map(attribute='src') | reject('search', '\.(ya?ml|json)$')
     | reject('search', '/\.gitkeep$') | list }}
```

The complement assert is what converts "we widened the pattern" into "the pattern
cannot silently go stale again". Widening alone is a point fix: the next file
extension someone invents is invisible again. Asserting that the directory contains
*nothing unread* means the guard fails loudly the first time reality grows past it —
including on the mundane case of a `telegraf-health.yaml.orig` backup left in a
directory that ships verbatim to a live Grafana.

Allow-list the inert residents explicitly, with the reason (here: one `.gitkeep`),
so the allowance is a decision on the record rather than a widened regex.

**And check the siblings.** This defect is a habit, not a typo: the same role holds
three older guards — rule grading, the per-file rule witness, and the notification
`group_by` reconciler — that still enumerate the alerting directory with the narrow
single-extension glob. They are not the deep-link guard and were out of scope for the
change that fixed this one, but they carry the identical hole, and a `.yml` rule file
would be invisible to all three. When a read-surface bug is found, grep the whole
module for the same pattern before calling it fixed.

### 2. Commit the fix, THEN prove it — a git-restoring harness reverts what it tests

The fault-injection harness applied one defect, ran the play, saved the log, and
restored with:

```bash
git checkout -- ansible
```

`git checkout -- <path>` restores to **HEAD**. The guard fix was still an uncommitted
working-tree change. So proof run 1 exercised the fixed guard, its restore step
deleted the fix, and runs 2, 3 and 4 silently exercised the **old** guard while
reporting on the new one.

It surfaced only because one class — the `.yml` half-pair — returned `rc=0` when it
had to fail, and that was chased rather than shrugged at. Had the injected defects
all been ones the old guard already caught, every run would have "passed" and the
harness would have certified the fix while testing its absence.

> **The failure mode of a broken proof harness is a PASS.** It does not error, it
> does not warn; it agrees with you.

The control is ordering, not vigilance: **commit the fix first, then run the proof
harness against the committed tree.** Then `git checkout --` restores *to the fix*,
which is what the harness needs anyway. Two supporting habits:

* Have the harness print the commit it is testing (`git rev-parse --short HEAD`) into
  every log it writes. A mismatch between "what I think I am proving" and "what HEAD
  says" then shows up in the artefact.
* Treat a proof that *passes* when you expected a failure as an incident. This one
  cost a re-run of eight classes; missing it would have shipped a guard whose only
  real test was the run that reverted it.

This is the fault-injection cousin of the repo's standing rule that a guard you have
not seen fail is not a guard. Seeing it fail is necessary; *seeing the right version
of it fail* is the part that is easy to lose — the same gap
[a guard demo that never exercises the branch it claims to test](../security-issues/guard-accept-condition-is-a-negative-fails-open.md)
fell into, and an instance of the broader rule that
[a verification is only evidence if its instrument can tell the fixed state from the broken one](verification-instrument-must-distinguish-fixed-from-broken.md).

### 3. A pinned, valid id is not necessarily a legal target

Grafana dashboards give **rows** ids, and rows consume ids from the same sequence as
panels. But `?viewPanel=<row id>` renders nothing — a deep link at a row is exactly
as dead as a link at an id that does not exist, and just as silent: the notification
still shows its "View panel" button.

The first guard treated "pinned in the JSON" as "linkable", which blessed the row
link. The live dashboards make the risk concrete: rows sit at
**1, 10, 20, 30, 40, 50, 60** on the host dashboards and **1, 4, 9, 12, 15** on the
NAS one, so every real link in the tree is one dropped digit from a dead one
(`11` → `10`).

The fix keeps two lists that are easy to conflate and must not be:

```yaml
# every id in the document — rows included, because rows CONSUME ids and the
# full-pinning check has to be made against all of them
_dash_ids_all: >-
  {{ _dash_panels | selectattr('id', 'defined') | map(attribute='id') | map('string') | list }}
# the row ids, computed separately rather than with rejectattr on `type`:
# a panel with no `type` key must survive this step
_dash_row_ids: >-
  {{ _dash_panels | selectattr('type', 'defined')
     | selectattr('type', 'equalto', 'row')
     | selectattr('id', 'defined') | map(attribute='id') | map('string') | list }}
```

and then stores `ids` (linkable = all minus rows) separately from `pinned`
(`_dash_ids_all | unique | length`, for the "every panel is pinned" equality). The
`unique` is not cosmetic either: without it, two panels hand-pinned to the same id
satisfy `ids | length == panels | length` and the dashboard is treated as fully
pinned while carrying an ambiguous target.

Generalised: when a guard checks that a reference resolves, check it against the set
of **legal targets**, not the set of **existing identifiers**. Those two sets differ
whenever the format gives ids to structural elements — rows, groups, folders,
sections.

### 4. `fileglob` is not recursive, and it globs basenames only

`query('fileglob', dir ~ '/*/*.json')` reads **zero files**. Ansible's `fileglob`
globs only the basename component and treats the dirname as a search path, so a
wildcard in the directory portion resolves nothing. The only symptom is a warning
that reads like a missing-file complaint:

```
[WARNING]: Unable to find '<dir>/*' in expected paths (use -vvvvv to see paths)
```

and then a task that loops over an empty list and reports `skipping`. Measured here:
the dashboard read returned **0 dashboards** on the first dry run, and the guard
would have judged every link against an empty map had the witness assert not caught
it.

Use `filetree` for anything that is a tree:

```yaml
{{ query('filetree', _obs_dashboard_dir)
   | selectattr('state', 'equalto', 'file')
   | map(attribute='src') | select('search', '\.json$') | sort }}
```

`filetree` walks recursively and returns each entry's absolute `src`, so a new
subdirectory is picked up with no edit — which is also what makes it match a
recursive loader like Grafana's provisioner.

### 5. Every guard needs a witness assert, and the witness is what caught two of these

None of the above would have been *recoverable* without the guard asserting that its
own inputs are non-empty and internally consistent, before asserting anything about
them:

```yaml
- _obs_link_rules | default([]) | length > 0
- _obs_dashboard_panels | default({}) | length > 0
- "'' not in (_obs_dashboard_panels | default({}))"
- _obs_dashboard_files | length == (_obs_dashboard_panels | default({}) | length)
- _obs_link_annotation_count | int > 0
```

The fourth line is the duplicate-uid detector: dashboards are accumulated into a dict
keyed by uid via `combine()`, so two files sharing a uid collapse into one entry —
last file sorted wins — and every link into that uid is then judged against the
**wrong file's** panels. Files-in must equal uids-out. The empty-string check catches
a dashboard JSON with no `uid` key at all, which Grafana would assign one to at
import, addressable by no alert link.

## Why This Matters

A static guard is trusted differently from a test. It runs on every deploy, nobody
reads its output when it passes, and its whole value is the assumption that a green
run means the defect class is absent. Every failure above preserves the green run.
That is the same shape as this repo's other expensive lessons — `failed_when: false`
turning an assert into `assert: true`, the `nfs=1` feature flag rejected every run
behind a silent-green `echo`, the `ping_group_range` carve-out — and it is why the
"prove it fails" step is not optional ceremony.

The read-surface rule in particular generalises past Ansible: any checker that
inspects "the files" while a shipper moves "the directory" is one file extension away
from being decorative. The complement assert is the cheap, durable form of the fix
because it fails on growth instead of silently ignoring it.

And the harness lesson is the sharpest of them, because it attacks the *evidence*
rather than the code: a proof procedure that can revert the thing it proves will
certify anything. Ordering (commit, then prove) removes the failure mode entirely, at
no cost.

## When to Apply

* Adding or editing any control-machine assert that parses files a remote service
  will load — Grafana provisioning, telegraf config fragments, compose files, nftables
  rulesets.
* Any time the deploy uses `synchronize`/rsync on a directory while the guard reads a
  glob. Ask: *what will the receiving process load out of this directory that my
  pattern does not match?*
* Before trusting a fault-injection run: is the fix committed? Does the harness log
  the SHA it tested?
* Whenever a guard validates a reference (id, uid, name) — enumerate the legal target
  set, not just the existing identifier set.
* Any `fileglob` whose pattern contains a `/` before the basename. Switch to
  `filetree`.

## Examples

The eight fault classes this guard is now known to catch, each injected into the
shipped tree, witnessed failing, and restored — **all re-run from scratch after the
harness ordering was fixed**, because the first run's results could not be trusted:

| Injected defect | Guard's response |
| --- | --- |
| `__panelId__` removed from a linked rule | `carries __dashboardUid__ without the other half` |
| `__panelId__: "999"` | `does not hold up against files/data/grafana/dashboards/` |
| `__panelId__: 33` (unquoted int) | assertion `_ann['__panelId__'] is string` |
| link at row id `10` | fails, and the message prints `linkable ids [...] and ROW ids [1, 10, 20, 30, 40, 50, 60]` |
| `zz-evil-181.yml` with a half-pair | now read: `Alert rule obs-issue181-half-pair carries __dashboardUid__ without the other half` |
| `telegraf-health.yaml.orig` left in the directory | `contains ['telegraf-health.yaml.orig'], which this guard does not read` |
| second dashboard JSON with a duplicate uid | `10 dashboard files yielding 9 uids` |
| two panels sharing one id | `34 panels, 33 distinct pinned ids` |

The reviewer's unmodified fixtures fail against the fixed logic as well
(`WITNESS failed: rules=3 from 2 files; dashboard files=3 -> uids=2`), and peeling the
faults apart one at a time on copies shows each is caught individually rather than
masked by whichever assert fires first — worth checking explicitly, because masking is
how a multi-clause guard quietly degrades into a single-clause one.

## Related Issues

- **#181** — the change this came from (deep links on every rule with an honest panel
  target, plus the gate).
- [grafana-alert-panelid-pairing-breaks-all-provisioned-rules](../integration-issues/grafana-alert-panelid-pairing-breaks-all-provisioned-rules.md)
  — the incident this gate exists to prevent. Its "Do not expect local tooling to
  catch it" section is now historically true rather than currently true: the failure
  is caught repo-side, under `--check`, before any write.
- **#230** and **#231** — the two out-of-scope findings surfaced while choosing link
  targets (six Home Assistant dashboards with zero pinned panel ids; no dashboard for
  `eq12_docker`, which is why four rules stay link-less).

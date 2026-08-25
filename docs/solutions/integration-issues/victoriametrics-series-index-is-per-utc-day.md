---
title: "VictoriaMetrics /api/v1/series is per-UTC-day, so a pinned window is not a pinned answer"
date: 2026-08-24
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "the identical pinned command against the identical pinned snapshot returns different counts hours later"
  - "a 60-second /api/v1/series window returns the same series set as the whole UTC day"
  - "union of two VictoriaMetrics endpoints grows 245 -> 300 while both query legs hold at 245"
  - "a report claims byte-for-byte reproducibility and every guard still passes while the headline numbers move"
root_cause: api_behavior
resolution_type: documentation_update
severity: medium
related_components:
  - victoriametrics
  - reconcile-ha-entities
  - home-assistant
tags:
  - victoriametrics
  - metricsql
  - reproducibility
  - inverted-index
  - measurement
  - right-by-condition
---

# A verification instrument whose own inputs drift with wallclock

## Problem

`scripts/reconcile-ha-entities.py` answers "which Home Assistant entities are
dead?" by reconciling HA's entity set against what VictoriaMetrics actually
stored over a pinned window. Its whole value rests on being a *measurement*, so
its docstring claimed that everything derived from VictoriaMetrics was a pure
function of `(--start, --end)` and repeated byte-for-byte, with HA's live
`/api/states` named as the only moving part.

That claim is false for one of the two VictoriaMetrics legs, and the instrument
had been publishing findings under it.

## Symptoms

Re-running the **identical** pinned command against the **identical** pinned HA
snapshot, 3.9 h after the published run, moved the VM-side counts — while the
query legs did not move at all:

```
                                       published (end+min)   replay (end+3.9h)
tlast_over_time pairs                          245                 245
count_over_time pairs (P3)                     245                 245
union: VM distinct (domain, entity_id)         245                 300
  listed by index, undated by query              0                  55
  dated by query, not listed by index            0                   0
writable AND VM-seen                           242                 251
VM-seen with no HA entity (vm_only)              0                  46
writable NEVER seen                            479                 470
```

Every guard still passed and the run still exited 0. Nothing announced that the
numbers a reader would paste had moved.

## What Didn't Work

**Assuming the code change caused it.** The drift surfaced while verifying an
unrelated commit, so the first hypothesis was the diff. Refuted by a control: the
**unmodified previous script**, run at the same instant on the same inputs,
produced the identical moved numbers. Its whole diff against the working tree at
that moment was new zero-valued counter lines, reworded prose and newly
enumerated names — **zero number differences**. The change was number-neutral;
the environment had moved.

**Correcting the claim once.** The first rewrite said the two query legs are pure
functions of the window and the dead list is built from them. The second half is
also false: `never_seen = writable_set - vm_seen` (`scripts/reconcile-ha-entities.py:1377`)
and `vm_seen = index_seen | set(last_sample)` (`:1359`) — the union includes the
index leg, so **the dead list is index-dependent too**. A caveat that names the
right mechanism and then draws the wrong boundary is still wrong.

**Reading the stub magnitude as the field magnitude.** With a stub of 55
synthetic index pairs, all of them writable HA entities, the dead list moved by
the full 55. That is the mechanism's upper bound, not an observation. In the
field the same 3.9 h replay moved the dead list by **9**, because 46 of the 55
real `index_only` pairs had no HA entity at all and landed in `vm_only`.

## Solution

Measured the endpoint directly, read-only:

```
/api/v1/series [Aug23 00:00Z, Aug24 04:06Z]  1601 series  1254 names  300 pairs
/api/v1/series [Aug23 00:00Z, Aug23 23:59Z]  1287 series   956 names  236 pairs
/api/v1/series [Aug23 00:00Z, Aug23 00:01Z]  1287 series   956 names  236 pairs  <- 60s window, same answer
max(tlast_over_time) over the pinned window                            245 pairs
```

**A 60-second window returns the same answer as the whole day.** `/api/v1/series`
resolves against a per-**UTC-day** inverted index, so a window ending mid-day
picks up that day's entire bucket — and that bucket keeps growing until the day
closes. The pinned window straddles two UTC days, so its answer is day-23 union
day-24, and day-24 was still filling.

The fix is not to the query. It is to every claim the instrument makes about
itself:

1. **Name the three moving sources, not one.** HA's `/api/states` (live), the two
   query legs (`tlast_over_time`, `count_over_time` — genuinely pure functions of
   the window), and `/api/v1/series` (not).
2. **Name which numbers the index leg touches**: `index_only`, `vm_only`, the
   series and metric-name counts, G4's series counts, G2's total, G3's hit rate,
   G6's VM-seen/string split, and the nine-device cross-check. Every one reads
   `vm_seen` or the raw series list.
3. **State the safety claim right by condition**, not by construction:
   - While the data is **retained**, `vm_seen` only grows as the trailing day
     fills, so a later re-run can only **shrink** the candidate list — it never
     convicts anyone new. Retention is the qualifier: aged-out samples or a
     deleted series shrink `vm_seen` and would **grow** the list.
   - The published 479 was window-pure **because** `index_only` read 0 in that
     run, which makes `writable - vm_seen` exactly equal `writable - tlast-seen`.
     That is a property of that run, checkable from its own printed counts — not
     a guarantee about every run.
4. **Print which case the run is in**, so the reader never has to take it on
   trust (`scripts/reconcile-ha-entities.py:1662-1676`): `THIS run's candidate
   list is NOT window-pure: index_only reads 55…` versus `…IS window-pure:
   index_only reads 0…`. The window-pure branch's follow-on clause reads P3's
   actual verdict rather than asserting it, because `index_only` is defined
   against the tlast set only (`:1360`) and it is P3 passing that extends the
   statement to the other query leg.
5. **Gate consequential verdicts on the leg that respects the window.** The
   nine-device `DISAGREE` verdict — this report's most consequential possible
   result — is now gated on a **dated** in-window sample rather than on
   membership of `vm_seen` (`:1882-1918`), with a separate
   `INDEX-LISTED (undated)` verdict for an index-only hit.

## Why This Works

The index leg's drift is **conservative for a dead list**: an after-window sample
is evidence of life, so every pair the growing index adds is a pair *removed*
from the candidate set. That is why the union stays — reading only the index, or
only the query, would each be worse. What was wrong was the claim of purity, not
the design.

The drift's own exonerations confirm the direction argument is real rather than
convenient. All **9** entities the later replay moved out of the dead list are
live in HA now, and all 9 produced their first sample **21–79 minutes after the
window closed** (04:28:28Z .. 05:25:41Z; the window ended 04:06:57Z). Four came
out of the published 310 numeric candidates and five out of the 169 string-only
INCONCLUSIVE ones, and the arithmetic reconciles exactly: `310 - 4 = 306` and
`169 - 5 = 164`, which is what the replay printed. An iPad that had not checked
in, an automation nobody had triggered, a battery sensor on a slow cadence:
"never seen in 28.1 h" mostly means "has not happened yet".

The 46 `vm_only` pairs are the same story from the other side: 23 exact
short-id/long-id twins of one Reolink camera integration, all 46 first sampled
after the window closed, the integration added ~45 min later and its device
renamed 27 s after that. Post-window creation plus a rename — not a dead device.

## Prevention

- **Run promptly after the window closes**, and capture the output then. A run
  made hours later reads a larger trailing bucket. It is not wrong, but it is not
  the same run, and the two must not be diffed as if it were.
- **Expect byte-identity only against an unchanged index state.** Pinning both
  the window and the upstream snapshot is necessary, not sufficient.
- **Isolate drift with a control before touching the diff.** Running the
  unmodified previous revision at the same instant on the same inputs is what
  separated "my change moved this" from "the environment moved". Judge the
  delta, never the absolute.
- **Ask which index an endpoint resolves against.** The one-command tell here
  costs nothing: query a 60-second window and a full-day window and compare. If
  they agree, the endpoint is not answering your window.
- **When a caveat names a condition, read the condition.** Two rewrites of this
  note were wrong because they asserted a premise instead of evaluating it; the
  printed line now evaluates `index_only` and `p3_ok` and says which case holds.

## Related

- [instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
  -- the parent doc, and the one this amends: its instrument table offers
  `/api/v1/series` for "has this series ever existed?", which is under-specified in
  a way that misleads. The endpoint answers "existed in the UTC day(s) overlapping
  the window", and its answer grows through the day for a fixed window.
- [truenas-26-api-exporter-configured-is-not-delivering](truenas-26-api-exporter-configured-is-not-delivering.md)
  -- the other VictoriaMetrics read-path surprise: a 204 push invisible at +3s and
  fine at +45s, so "pushed but not queryable" is not evidence of a broken writer.
- [verification-instrument-must-distinguish-fixed-from-broken](../conventions/verification-instrument-must-distinguish-fixed-from-broken.md)
  -- do not inherit a measurement without re-deriving what it measured.
- [absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md)
  -- for anyone building an absence or coverage decision on a windowed series set.
- [home-assistant-last-changed-rewritten-at-restore](home-assistant-last-changed-rewritten-at-restore.md)
  -- the sibling #171 finding: which timestamp source is trustworthy on the HA side.

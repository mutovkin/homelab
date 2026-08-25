---
title: "Home Assistant rewrites last_changed at restore, so platform metadata cannot date a device's death"
date: 2026-08-24
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "0 of 574 not-yet-seen entities report a last_changed older than 7 days, including a device dead since 2023-08-20"
  - "all 49 select entities report last_changed inside the window while VictoriaMetrics holds 5 select series on the index leg (27 when re-read 3.9 h later)"
  - "HA serves a live-looking state (23.3) for a sensor VictoriaMetrics has never seen in-window"
  - "a dead-device list built from platform metadata looks entirely plausible and is entirely wrong"
root_cause: api_behavior
resolution_type: tooling_addition
severity: medium
related_components:
  - home-assistant
  - victoriametrics
  - reconcile-ha-entities
tags:
  - home-assistant
  - victoriametrics
  - timestamps
  - restore
  - dead-device-detection
  - positive-control
  - pre-registration
---

# Platform metadata cannot date a device's death — the TSDB's sample timestamps can

## Problem

"Which Home Assistant entities are dead?" has an obvious-looking answer: read
`last_changed` off `/api/states` and sort. That answer is worthless, because
**HA re-stamps `last_changed` when it rebuilds state at startup**. Every entity in
a restored instance carries a fresh-looking timestamp, including the ones that
stopped reporting years ago, and nothing in the payload distinguishes the two.

Stated precisely, because the distinction matters for what follows: the rewrite is
HA's documented behaviour, and what is measured *here* is its EFFECT -- the field
is uniformly recent and cannot discriminate. No restart or restore event was
instrumented in this work.

The honest source is the time-series database. VictoriaMetrics stores the
*sample* timestamp, which no restore rewrites.

## Symptoms

- Every not-yet-seen entity reports a plausible recent `last_changed`, so the
  dead ones are indistinguishable from the live ones. **Measured: 0 of 574
  not-yet-seen entities read older than 7 days**, including a device whose
  independent `last_seen` series put its death at 2023-08-20 — **1098 days**.
  (That 574 is the not-yet-seen population of an earlier, narrower probe in #171
  -- the one carried in this script's own module docstring, dated 2026-08-24 --
  not of the run below, which reports 479 over its 28.1 h window. Two
  measurements, two windows: do not read them as one number moving. The earlier
  probe's window is not recorded here, so it is cited as a separate measurement
  rather than compared.)
- HA serves a *live-looking state value* for hardware that has been silent for
  years: `sensor.stairs_sensor_air_temperature` reads `23.3`,
  `sensor.front_door_motion_sensor_humidity` reads `54.84`, while
  VictoriaMetrics has never seen either in-window.
- A whole domain claims to have just changed. **All 49 `select` entities
  reported `last_changed` inside the window — 49 of 49, 100%** — while VM held
  only 5 `select` series over that window. That is not credibly 49 state changes
  against 5 series -- the signal the freshness check reads cannot tell a rewrite
  from a change, which is the whole problem. (Stated exactly as measured: 49 entities against 5 series, not 5
  entities — the script prints series, and the mapping from series to entities
  was never measured. The same line read 27 series when re-run 3.9 h later,
  because the series count comes from the day-granular index leg; see
  [victoriametrics-series-index-is-per-utc-day](victoriametrics-series-index-is-per-utc-day.md).)

The instance was otherwise healthy, which is what makes this a trap rather than
an obvious anomaly: the run's restore-burst indicator read **57/1213 (4.7%)** of
all entities changed within the same 30 minutes — a normal figure — yet all nine
known-dead devices sat inside that rewritten population wearing recent
timestamps.

## What Didn't Work

Four failure modes, each of which produced a plausible-looking wrong answer.

**Joining on the bare `entity_id` label.** The VM label carries the *object id*
only, not the domain, so a diff joined on `entity_id` alone reports the entire
instance as dead — and the output looks entirely reasonable. The positive
control that catches it (G3, bounds at `scripts/reconcile-ha-entities.py:186-189`):
numeric-capable entities that genuinely changed state just before the window end
MUST be VM-seen.

```
G3 healthy         50/50 = 100.0%
G3, join broken     0/50 = 0.0%   writable AND VM-seen 0, vm_only 244, exit 2
                    (a separate fault-injection run; the published run's VM
                     distinct pair count is 245)
                    (a separate fault-injection run; the published run's VM
                     distinct pair count is 245)
```

**Substring device matching.** `stairs_sensor` is a substring of
`upstairs_sensor` and `downstairs_sensor`. A live sibling matching by accident
sets the "VM saw it" flag and manufactures the DISAGREE verdict — the most
consequential result this report can produce. The match is anchored instead
(`scripts/reconcile-ha-entities.py:1845-1847`, `obj == device or
obj.startswith(device + "_")`); measured against this instance, anchoring
changes 98 matches to 98.

**An expect-zero guard whose query is broken also returns zero.** G4 asserts 0
series with `domain="update"`. On its own, "0 because absent" and "0 because the
query is broken" are the same number, so it is paired with a positive control
running the SAME helper with the same argument shape against `domain="sensor"`:
585 series in the published run. Without that, the guard's healthy answer and
its broken answer are indistinguishable.

**The exculpatory hypothesis was refuted, not confirmed.** Pre-registered
theory: a state that is neither `float()`-able nor in HA's binary vocabulary can
only be stored as a string field, so such an entity could be alive and
structurally invisible to a numeric-only reconciliation
(`scripts/reconcile-ha-entities.py:450`). Measured, it does **not** hold: **70 of
the 245 VM-seen entities carry a non-numeric-capable state**, so string states
ARE written. The rule was applied as pre-registered anyway — a rule fixed before
a run does not get rewritten after seeing the data — but the refutation cuts
toward MORE suspicion of that group, not less: their absence is no longer
excused by mechanism, merely unranked. They are now enumerated by name rather
than summarised as per-domain counts.

## Solution

Date liveness from the TSDB's sample timestamps.
`max(tlast_over_time({db="ha"}[<window>s])) by (domain, entity_id)` returns the
SAMPLE timestamp, not the query time — that is the whole point
(`scripts/reconcile-ha-entities.py:1058-1060`).

Absence is evidence only inside a stated window: an entity absent from the TSDB
is evidence of death **only if it was expected to report inside that window**.
So every conclusion carries the window, and the output is a candidate list, not
a verdict list.

Over a 28.1 h window (`2026-08-23T00:00:00Z .. 2026-08-24T04:06:57Z`):

```
HA entities total                      1213
  excluded by design (domain=update)     70
  absent by design (unavail/unknown)    422   HA never writes these: event_to_json -> None
  writable                              721
VM distinct (domain, entity_id)         245
writable AND VM-seen                    242
writable NEVER seen                     479
  numeric-capable state (candidates)    310
  string-only state (INCONCLUSIVE)      169
```

The two by-design subtractions are numbers, not assertions. The 422 are states
HA never writes at all — `event_to_json()` returns `None` for `unavailable` and
`unknown` (`scripts/reconcile-ha-entities.py:141-142`), so their absence from the
TSDB says nothing about the device.

**Candidates bounded by the window, not convictions.** Silence for these 310 is
bounded BELOW by 28.1 h and above by nothing. A door nobody opened, a sprinkler
zone that runs every third day, an automation nobody triggered — all read "never
seen" and all are dormant, not dead. The operator's judgement about expected
cadence is what turns a candidate into a dead device, and this run cannot supply
it.

## Why This Works

Two mechanisms, derived independently, agreed on every device they both cover.
Nine devices identified from a `last_seen` series were cross-checked against the
sample-timestamp mechanism: **nine AGREE, zero DISAGREE**, and every one was the
stronger form — HA still reports a live state while VictoriaMetrics has never
seen the entity in-window. **Not one of the 98 entities belonging to those nine
devices produced a single sample in 28.1 h.**

That is the finding stated precisely: HA is not merely *wrong about the date*.
It serves a stale reading as a current one. A dashboard built on `last_changed`
shows a house full of healthy sensors; the TSDB shows which ones stopped talking
in 2023.

The design that makes the agreement meaningful rather than circular is that the
two mechanisms share no inputs — one reads a `last_seen` series, the other reads
sample timestamps of every series — so a disagreement would have been the
finding. There was none.

## Prevention

- **Date liveness from the TSDB, never from platform metadata a restore can
  rewrite.** This applies beyond HA: any platform that reconstructs "last
  changed" from its own restart is unusable as a clock.
- **State the window in every conclusion.** An absence finding without a window
  is not a finding. Related: [instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md).
- **Pre-register the exculpatory rule before the run, and keep it when the data
  refutes it.** Rewriting the rule after seeing the data is how a measurement
  becomes an argument. Record the refutation and note which direction it cuts.
- **Pair every expect-zero guard with a positive control on the same code
  path.** A broken query and a true zero produce identical output otherwise.
- **Anchor name matching** on `==` or `<name>_` prefixes. A bare substring will
  eventually match a live sibling and manufacture your most consequential
  verdict.
- **Pin BOTH sides if a re-run is meant to reproduce.** HA's `/api/states` is a
  live source: measured drift of roughly **11 entities/min**, which showed up as
  the only two lines that moved across four pinned re-runs (the G3 control size
  and the restore-burst indicator). Pinning the window alone is not enough;
  pinning the HA readout alone is not either.

## Related

- [vector-hostname-and-severity-labels-were-fabricated](vector-hostname-and-severity-labels-were-fabricated.md)
  -- the nearest sibling: `_time` was ingest time and `hostname` was the container
  id, the same "the platform stamped its own identity over the event's" failure. A
  label that is wrong-but-present is worse than a missing one, which is exactly why
  `last_changed` is worse than no field at all.
- [truenas-26-api-exporter-configured-is-not-delivering](truenas-26-api-exporter-configured-is-not-delivering.md)
  -- `host` is the forwarder's name stamped on metrics it merely received, and
  `_devicename_sda` reports a fabricated `0` where the API returns `null`. Classify
  on the honest discriminator.
- [instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
  -- an instant query's answer is stamped with the QUERY time, not the sample's; the
  sample's own timestamp is the honest source.
- [absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md)
  -- the `select`-domain finding is the presence-side twin of the always-absent
  counter: a signal that cannot discriminate.
- [victoriametrics-series-index-is-per-utc-day](victoriametrics-series-index-is-per-utc-day.md)
  -- the sibling #171 finding: which VictoriaMetrics endpoint is trustworthy, and why
  the `select` series count above moved when the same window was re-read.

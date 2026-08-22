---
title: "An absence alert is only as good as the series it watches: prove continuous presence, or pick a sentinel that has it"
date: 2026-08-22
category: conventions
module: services/observability
problem_type: convention
component: tooling
severity: high
applies_when:
  - "writing any alert rule whose firing condition is the absence or staleness of a signal"
  - "setting noDataState to Alerting on a Grafana rule"
  - "choosing which series an ingest-stalled, lag, or dead-man's-switch rule should watch"
  - "reviewing a rule that pages continuously, or one that has never fired and cannot"
  - "adding a deploy-time assert that demands traffic from something that may legitimately be quiet"
symptoms:
  - "A rule fires every evaluation on a healthy system, and widening its window only delays it"
  - "A rule reports Normal forever because its query returns no data and NoData is treated as healthy"
  - "A counter used as an absence signal turns out to be absent on an idle system"
related_components:
  - grafana
  - victoriametrics
  - victorialogs
  - telegraf
  - vector
  - rsyslog_structured
tags:
  - alerting
  - absence
  - nodata
  - heartbeat
  - prometheus
  - victoriametrics
  - silent-green
  - false-alarm
---

# An absence alert is only as good as the series it watches: prove continuous presence, or pick a sentinel that has it

## Context

This project has now made the same mistake three times, in three subsystems, with two
opposite symptoms. It is worth one doc because the *shape* is identical every time and
the shape is not obvious from any single instance.

**The rule.** An alert that fires on absence is a claim about a series: *in health, this
series is continuously present, so its disappearance means something is broken.* That
claim is a **measurement**, not a property you get for free by choosing a
sensible-looking metric. If it is false, the rule has exactly two failure modes and no
third:

- the series is legitimately absent sometimes, so the rule **pages constantly** in perfect
  health; or
- absence is treated as healthy (`noDataState: OK`), so the rule **can never fire**, and
  a rule that cannot fire is worse than no rule because it reads as coverage.

Both are silent-green failures in the sense this repo uses the term: the routine evidence
of health keeps reporting success while a control does nothing.

## Guidance

**Before writing an absence or staleness rule, sample the series and prove it is
continuously present in health. If it is not, do not tune the rule — change the series.**

Three concrete moves, in order of preference:

1. **Measure first.** For a log or event stream, the decision statistics are
   **empty-window fraction** at the rule's own granularity and **max gap**. Not gap
   percentiles — see below, they actively mislead.
2. **Manufacture the signal.** If the thing you care about is naturally intermittent,
   emit a heartbeat so absence becomes unambiguous. A guaranteed floor turns
   `count() < 1` from "this host was quiet" into "not one line, not even the beat".
3. **Pick a sentinel.** If the *metric* you care about is not continuously exported, watch
   a different series that is and that travels the identical collection path. Then record
   the coupling **at both ends**, because nothing enforces it.

And a fourth, which is really a warning: **do not validate any of this with a bare
instant query.** VictoriaMetrics and Prometheus answer an instant query from a ~5-minute
lookback and stamp the result with the *query* time, so a series that stopped four minutes
ago is indistinguishable from a live one. That instrument returns a false positive for
exactly the condition an absence rule exists to detect — see
[instant-query-cannot-prove-a-series-is-live](instant-query-cannot-prove-a-series-is-live.md).
It is the reason the third instance below was nearly shipped.

## Why This Matters

### Instance 1 — a rule that paged every 30 minutes for 21 hours (#134)

`obs-host-log-ingest-stalled` asked `_time:10m source:host | count() < 1`. It looked
obviously safe: these hosts log continuously. Measured 2026-08-20, in perfect health
(the table is recorded in `ansible/roles/rsyslog_structured/README.md` and, minus the 15m
column, in the rule's own comment — this doc is the general lesson, those are the
operational copies):

| host | empty 10m windows | empty 15m | max gap |
| --- | --- | --- | --- |
| `eq12_docker` | **66.2%** (VictoriaLogs: 68.1%) | 57.5% | 3600.0s |
| `eq12` | **39.7%** | 33.8% | 3600.0s |
| `n5pro` | 0.0% | 0.0% | 448s |
| `n5pro_docker` | 0.0% | 0.0% | 396s |

Two thirds of windows on the busiest host were legitimately empty. Host logs arrive in
**bursts**: one 10-minute bucket held 1288 records, another held 1. Burstiness did not
track volume — the busiest host was the worst offender.

Two lessons hide in that table:

- **Gap percentiles mislead.** `eq12_docker`'s p99 gap was 15.6s (recorded in CLAUDE.md's
  gotcha for this lesson) while two thirds of its windows were empty. Only max gap and
  empty-window fraction are decision-relevant.
- **Widening the window is not a fix.** The max gap on both offending hosts is *exactly*
  3600.0s — that is not headroom, it is one incidental hourly event holding the window
  open. A 60-minute window would have been one bugfix away from flapping again, at the
  cost of ~70 minutes of detection latency.

The fix was a heartbeat, not a threshold: one marker per host per five minutes, which
gives a healthy host a floor of ~2 records per 10-minute window and makes the original
query meaningful again.

### Instance 2 — a deploy assert demanding traffic from silent containers (#134)

The same batch shipped an assert that required recent container log records on hosts
whose containers were legitimately idle. Same error, different layer, and found only
because instance 1 had just taught the team to look.

### Instance 3 — a counter that does not exist until it is non-zero (#152)

Reviewing #152, a proposed absence owner for the delivery telemetry was:

```
min(lag(grafana_alerting_notifications_total[24h])) > 600     noDataState: Alerting
```

The reasoning was that telegraf scrapes and writes that series every 60s regardless of
whether it increments. Telegraf can only write what Grafana **exports**, and a Prometheus
counter is not exported until something increments it — this one only while notifications
are actually being sent. Sampled every 30s for 4.5 minutes on a healthy idle Grafana:

| series | samples containing it |
| --- | --- |
| `grafana_alerting_notifications_total` | **0 of 9** |
| `grafana_alerting_alertmanager_receivers` | 9 of 9 (2 series) |
| `grafana_alerting_active_configurations` | 9 of 9 (1 series) |

And in VictoriaMetrics, `count(grafana_alerting_notifications_total)` over 80 minutes was
present only from 05:23 to 05:29 and absent for the rest of a completely healthy hour —
same Grafana process throughout.

That rule would have fired permanently from the moment it landed. It was rebuilt on
`grafana_alerting_alertmanager_receivers`: continuously exported for the process
lifetime, travelling the identical scrape path (Grafana `/metrics` → telegraf →
VictoriaMetrics), so its absence really does mean the path is broken. Its value is a
bonus signal — `state="active"` should equal the provisioned receiver count.

Note what changed and what did not. The *rule's job* was right and worth having: without
an absence owner, the delivery-failure rule's healthy state and its blind state are the
same state (`NoData` → `OK`), so a dead telegraf leaves it reporting Normal forever. Only
the series was wrong.

### The coupling has to be written down at both ends

The sentinel is now load-bearing in a place nothing checks: telegraf's filter carries it
as an **exact** name alongside a glob that does *not* cover it.

```toml
fieldpass = ["grafana_alerting_notifications*", "grafana_alerting_alertmanager_receivers"]
```

"Tidying" those into one pattern silently blinds the absence owner — the one failure it
cannot report about itself. So the rule's annotation names both entries and says which
one is its own feed, and the telegraf config says the same thing from its side. An
invariant that lives in only one of two files is a future edit away from being false.

## When to Apply

- Any rule with `noDataState: Alerting` — ask what makes the series present in health, and
  whether that has been measured.
- Any rule with `noDataState: OK` — ask whether it can ever fire, and which rule owns
  absence for that family. Absence should be alerted on exactly once; the other rules in
  the family then treat NoData as healthy, and that is only safe because the owner exists.
- Any `count() < n` over a window, on any store.
- Any deploy-time assert that demands recent traffic from something that could be quiet.
- Whenever a counter is chosen as evidence: counters that are only exported while non-zero
  are common, and Grafana's alerting counters are the worked example.

## Examples

Measuring empty-window fraction before trusting a threshold — the check that would have
prevented instance 1, and that cleared the docker twin added in #154:

```bash
# 10-minute buckets across an ALIGNED 7-day range. Alignment is the trick: a known
# bucket count means "buckets returned" vs "buckets expected" IS the empty-window count,
# with no second query. 7d / 10m = 1008.
curl -s -u "$VL_AUTH_USERNAME:$VL_AUTH_PASSWORD" \
  http://localhost:9428/select/logsql/query \
  --data-urlencode "query=_time:[<start>, <start+7d>] source:docker | stats by (_time:10m) count() as rows"
# measured 2026-08-21 on eq12_docker:
#   1008 buckets expected, 1008 returned, 0 empty, minimum 712 records
```

`source:docker` cleared it with a ~712x margin, so `count() < 1` is meaningful there.
`source:host` did not, which is why it needed a heartbeat instead. Same query shape, two
different verdicts, decided by measurement rather than by how continuous the source
*looked*.

Checking whether a metric is continuously exported before building an absence rule on it:

```bash
# Sample the exporter directly — the TSDB's lookback would hide the gaps.
for i in $(seq 1 9); do
  docker exec telegraf wget -q -O - http://grafana:3000/metrics 2>/dev/null \
    | grep -c '^grafana_alerting_notifications_total'
  sleep 30
done
# 0 0 0 0 0 0 0 0 0   -> cannot carry an absence rule
```

## Related

- [instant-query-cannot-prove-a-series-is-live](instant-query-cannot-prove-a-series-is-live.md)
  — the instrument half. Validating any of the above with a bare instant query gives a
  false "yes" for a series that has just died.
- [grafana-alerting-provisioned-but-undeliverable](../integration-issues/grafana-alerting-provisioned-but-undeliverable.md)
  — #139, the immediate predecessor. Its "absence should be alerted on exactly once" rule
  is what this completes: the owner's own series must itself be continuously exported.
- [prove-notification-delivery-not-just-config-validity](prove-notification-delivery-not-just-config-validity.md)
  — the same manufacture-the-signal instinct one layer up: force a send rather than
  waiting for one, and rank the evidence honestly.
- [verification-instrument-must-distinguish-fixed-from-broken](verification-instrument-must-distinguish-fixed-from-broken.md)
  — "absence means something only after presence has been demonstrated" is the closest
  prior sentence in this store; this doc is that sentence with the measurements attached.
- [vector-057-silent-log-pipeline-failure](../integration-issues/vector-057-silent-log-pipeline-failure.md)
  — the origin of the silent-green family. Its `_time:5m` recency check is itself an
  absence check of the vulnerable shape: fine as a post-fix spot check, not as an
  alerting recipe on a bursty source.
- [vector-hostname-and-severity-labels-were-fabricated](../integration-issues/vector-hostname-and-severity-labels-were-fabricated.md)
  — a sibling absence-measurement error: absence measured through a label schema the
  change itself introduced, which could not distinguish "never happened" from "happened
  under the old labels".
- `CONCEPTS.md` → **Absence-owning rule** for the vocabulary.
- Issues #134 (instances 1 and 2), #152 (instance 3), #154 (the docker twin that cleared
  the pre-gate), #151 and #160 (more absence-shaped rules).

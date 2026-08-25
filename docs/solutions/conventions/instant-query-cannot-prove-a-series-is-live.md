---
title: "An instant query cannot prove a series is live: it answers from a lookback window, and stamps the answer with the query time"
date: 2026-08-22
last_updated: 2026-08-24
category: conventions
module: services/observability
problem_type: convention
component: tooling
severity: high
applies_when:
  - "writing or validating an alert rule whose condition is absence, staleness, or lag"
  - "checking by hand whether a metric is still being written"
  - "confirming that a collector, exporter, or remote-write path is alive"
  - "a runbook step says to curl /api/v1/query and read the answer as proof of health"
  - "reporting that a counter incremented, and inferring from that that the series is current"
symptoms:
  - "An instant query returns a value, and that is written up as the series being live right now"
  - "A metric looks present for several minutes after its exporter stopped exporting it"
  - "A query_range over the same window disagrees with the instant query that was used to confirm health"
related_components:
  - victoriametrics
  - grafana
  - telegraf
  - vector
tags:
  - victoriametrics
  - promql
  - metricsql
  - lookback
  - staleness
  - absence-rule
  - verification
  - silent-failure
---

# An instant query cannot prove a series is live: it answers from a lookback window, and stamps the answer with the query time

## Context

While landing #152 we needed one specific fact: is `grafana_alerting_notifications_total`
written continuously, or only sometimes? The answer decides whether an absence rule can
be built on it. So we queried it, got a value back, and moved on.

That was wrong, and the way it was wrong is the point. The instrument used to check
whether a series was still being written **cannot distinguish a live series from one
that stopped four minutes ago** — and it was being used to validate an alert rule whose
entire job is detecting that a series stopped.

This is
[verification-instrument-must-distinguish-fixed-from-broken](verification-instrument-must-distinguish-fixed-from-broken.md)
applied to a query engine. There the instrument was Ansible's check mode and rsync's
itemize output; here it is the query itself, and its default lookback is what erases the
distinction.

## Guidance

**An instant query (`/api/v1/query`) returns the most recent sample within a lookback
window — about five minutes by default in VictoriaMetrics and Prometheus. Inside that
window, a dead series and a live one return byte-identical output.**

Worse, and this is the part that removes the last chance to notice: **the result is
stamped with the *query* time, not the sample's time.** There is nothing in the response
to say the value is stale. Measured on the live stack:

```
wallclock now                                                    1787382867

/api/v1/query?query=grafana_alerting_alertmanager_receivers
  -> "value":[1787382867, "4"]                                   <- the QUERY time

/api/v1/query?query=timestamp(grafana_alerting_alertmanager_receivers)
  -> "value":[1787382867, "1787382780"]                           <- the SAMPLE time
                                                                     87 seconds older
```

Both lines describe the same sample. Only the second one tells you when it was written.

So read an instant-query hit as **"a sample exists no older than the lookback window"** —
never as "the series is live". Those are different claims, and only the first one is
supported. (The evidence-ladder framing in
[prove-notification-delivery-not-just-config-validity](prove-notification-delivery-not-just-config-validity.md)
is the same discipline: the failure is writing a lower rung as a higher one.)

To ask whether a series is *still being written*, use an instrument that can answer it:

| question | instrument |
| --- | --- |
| Is this series still being written? | `query_range` over the window, or `count_over_time(m[10m])` |
| How old is the newest sample? | `timestamp(m)`, compared against now |
| Has this series *ever* existed? | `/api/v1/series?match[]=m` (but see the caveat below), or the value itself for a monotonic counter |
| Is it live *right now*, at the source? | the exporter's own `/metrics` endpoint, not the TSDB |

Caveat on that third row, measured 2026-08-24 (#171): `/api/v1/series` resolves
against a per-**UTC-day** inverted index, so it answers neither "ever" nor "within
my window" -- it answers "existed in the UTC day(s) overlapping the window", and
its answer GROWS through the day for a *fixed* window (the same pinned window read
245 pairs at one moment and 300 pairs 3.9 h later, while both query legs held at
245). For this endpoint the caller's window is not the instrument's window. See
[victoriametrics-series-index-is-per-utc-day](../integration-issues/victoriametrics-series-index-is-per-utc-day.md).

## Why This Matters

The failure is not that the query is imprecise. It is that the imprecision points the
wrong way for the one job you were using it for.

An absence rule fires when data stops. To trust one, you must first establish that the
series is continuously present in health. If you establish that with an instant query,
you get a "yes" for any series that was alive within the last five minutes — including
one that has just died, which is exactly the state the rule exists to catch. **The
instrument returns a false positive for precisely the condition under test.**

Measured during #152, in order:

```
instant query, evaluated 05:25:00Z   -> series returned (epoch 1787376300)
instant query, evaluated 05:31:35Z   -> series STILL returned (epoch 1787376695)

query_range count(...), step 120s    -> buckets at 05:23, 05:25, 05:27, 05:29
                                        nothing from 05:31 onward

instant query, 05:56:49Z             -> "result":[]
grafana /metrics, 05:56:49Z          -> the family absent
```

Read the middle two lines together, because that pair *is* the trap: at 05:31:35 the
instant query returned a value while the range grid, over the same region, already showed
nothing after 05:29. Both queries were right. They answer different questions, and only
one of them was the question being asked.

The 05:31:35 reading was written up as confirmation that the delivery counters were
arriving. What it actually established is narrower and still useful — that a notification
had been sent within the lookback, which is genuine delivery evidence. The claim it was
mistaken for, *this series is being written*, was already false, and stayed undetected
until a `query_range` was run over the same window for an unrelated reason.

Only then did the real behaviour surface: Grafana exports that counter transiently, so
an absence rule built on it would have fired permanently. That finding is documented
separately; it was reachable only after the instrument was fixed.

The repo also contained two runbook recipes that teach the trap, and checking them turned
up something worse than staleness — both are inert:

```
# PORT_REFERENCE.md, "Test VictoriaMetrics query"
/api/v1/query?query=up                    -> {"result":[]}   on a HEALTHY stack
```

`up` is a metric a *scraper* synthesises about its own targets. Nothing in this stack
produces one: telegraf and vector both push by remote write, and telegraf's single
`[[inputs.prometheus]]` scrape emits no `up` series (and carries a `fieldpass` that would
drop it anyway). So `up` has never existed here, and the smoke test returns an empty
result whether VictoriaMetrics is healthy or dead.

```
# PORT_REFERENCE.md, billed as "the only one that would have caught #133"
/api/v1/query?query=vm_rows_inserted_total | grep influx    -> {"result":[]}   always
```

`vm_rows_inserted_total` is one of VictoriaMetrics' **own** internal metrics. It is served
on `:8428/metrics`, but the container runs with no self-scrape flag, so it is never
ingested into the TSDB it is being queried from. The check billed as the one that would
have caught #133 returns an empty result regardless of whether any InfluxDB client has
ever written.

Read off the endpoint instead, the metric works exactly as intended — `{type="influx"}` is
`0` while `{type="promremotewrite"}` is `2141837`, every other type `0`. The value does
distinguish never-started from has-written; it was only ever the *query form* that was
broken.

> An aside that belongs in this doc rather than a footnote: the first draft of this
> paragraph said "every `type=` label present, all at `0`", because the measurement was
> read off a `head -5` and the sixteen `type=` labels are alphabetical —
> `promremotewrite`, the one that is not zero, sorts thirteenth. Generalising from a
> truncated read is the same error as generalising from a lookback-shortened one, made by
> hand instead of by a query engine, in a doc about not doing that. Caught in review by
> someone re-running the command without the `head`.

Both recipes are corrected in the same change as this doc.

## When to Apply

- Before writing any alert rule whose condition is absence, staleness, `lag()`, or
  `count() < n` — establish continuous presence first, with a range query.
- Whenever a check's conclusion is "the pipeline is alive" and its evidence is a single
  `/api/v1/query`.
- When writing a runbook step: prefer `count_over_time` or the exporter's `/metrics` to a
  bare instant query, and say which question the step answers.
- When a metric is served by a component's own `/metrics` but nothing scrapes it into the
  TSDB — then no query against the TSDB can see it at all, and the endpoint is the only
  source.

## Examples

Establishing that a series is continuously present, before trusting an absence rule on it:

```bash
# WRONG — returns a value for anything alive in the last ~5 minutes
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  "http://localhost:8428/api/v1/query" --data-urlencode "query=$METRIC"

# RIGHT — count the buckets that actually contain samples across the window.
# An empty bucket is the thing you are looking for; an instant query cannot show it.
END=$(date -u +%s); START=$((END-86400))
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  "http://localhost:8428/api/v1/query_range" \
  --data-urlencode "query=count($METRIC)" \
  --data-urlencode "start=$START" --data-urlencode "end=$END" \
  --data-urlencode "step=120"
```

Asking how stale the newest sample is, rather than whether one exists:

```bash
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  "http://localhost:8428/api/v1/query" \
  --data-urlencode "query=time() - timestamp($METRIC)"   # seconds since the last write
```

The same discipline already exists one layer up, in
[grafana-alerting-provisioned-but-undeliverable](../integration-issues/grafana-alerting-provisioned-but-undeliverable.md):
rule liveness is checked by reading `lastEvaluation` **twice** and requiring it to
advance, rather than by reading it once and finding it non-empty. That is a range check
against the API; this is the same check against the TSDB.

## Related

- [verification-instrument-must-distinguish-fixed-from-broken](verification-instrument-must-distinguish-fixed-from-broken.md)
  — the parent thesis. Before trusting a verification, ask what its output would look like
  if the thing had *not* worked, and confirm that is a different output. Here it is not.
- [grafana-alerting-provisioned-but-undeliverable](../integration-issues/grafana-alerting-provisioned-but-undeliverable.md)
  — same stack (#139); its `lastEvaluation` must-advance check is the API-layer analogue,
  and its "absence should be alerted on exactly once" rule is the rule-design half.
- [prove-notification-delivery-not-just-config-validity](prove-notification-delivery-not-just-config-validity.md)
  — the evidence ladder. An instant-query hit is a real rung, just a lower one than it
  gets written as.
- [vector-hostname-and-severity-labels-were-fabricated](../integration-issues/vector-hostname-and-severity-labels-were-fabricated.md)
  — the LogsQL mirror image, in its superseded-by-#153/#154 note: a query whose *window*
  or *label schema* makes real history unreadable. That one produced a false **absence**;
  this one produces a false **presence**. Together: a query is an instrument, and its
  window is part of the instrument.
- `ansible/roles/services/observability/README.md` — the rule-specific consequence for
  `obs-alert-delivery-telemetry-absent` lives there and links here; this doc is the
  general lesson and owns the measurements.
- Issues #152 (where it surfaced), #151 and #160 (more absence-shaped rules whose
  verification needs this), #133 (owns the corrected PORT_REFERENCE recipes).

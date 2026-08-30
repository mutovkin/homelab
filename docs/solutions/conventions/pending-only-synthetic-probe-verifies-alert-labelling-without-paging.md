---
title: "Verify an alert rule's per-instance labelling without paging anyone: push a synthetic series into the rule's `for:` headroom and let staleness kill it first"
date: 2026-08-29
category: conventions
module: services/observability
problem_type: convention
component: monitoring
severity: high
applies_when:
  - "a change alters what labels an alert rule's instances carry (adding or removing a by-clause label)"
  - "you need to prove an alert would NAME the right host, disk, or component before trusting it"
  - "a reviewer asks for evidence that a regrouped rule produces one instance per member instead of one merged instance"
  - "the only convincing proof would normally require making a real alert fire"
symptoms:
  - "A config diff shows the by-clause changed, which proves nothing about the instances Grafana will actually produce"
  - "The obvious way to prove per-instance labelling is to push a synthetic sample over threshold, which is exactly what paged the operator at 3am in #201"
  - "A rule looks correctly grouped in the provisioning file but its live alert instances are the only evidence that counts"
related_components:
  - grafana
  - victoriametrics
  - vector
tags:
  - grafana
  - alerting
  - victoriametrics
  - verification
  - synthetic-probe
  - staleness
  - safety
---

# Verify an alert rule's per-instance labelling without paging anyone

## Context

#201 recorded the trap in full: verifying #188 meant proving that two drives crossing a
thermal threshold produce **two** notifications rather than one merged alert naming neither.
The only honest way to prove *delivery* is to make alerts actually fire and count what
arrives — and that is precisely what it did, at ~3am, on the operator's phone, in a message
indistinguishable from a real incident.

#217 needed a weaker but very common thing: proof that a regrouped rule
(`max by (component_id)` → `max by (host, component_id)`) really produces one alert
instance **per host**, each carrying a `host` label the summary can interpolate. That is a
question about **labelling**, not about delivery — and labelling is fully visible in the
`Pending` state, which sends nothing.

So the drill can be made structurally incapable of paging, rather than merely careful.

## Guidance

Push the synthetic series for a bounded window, then stop, and let the datasource's
staleness window remove the series **before** the rule's `for:` duration elapses. The
instance reaches `Pending` — where its labels are readable — and can never reach `Alerting`,
because the condition stops being true first. The safety is arithmetic, not vigilance.

The bound to satisfy:

```
last_push + query_staleness  <  first_pending_eval + rule.for
```

`first_pending_eval` is the **first evaluation tick after the first push**, which in the
worst case is immediately after it. Substituting that worst case gives the usable form:

```
push_window  <  rule.for − query_staleness
```

For #217's rule (`for: 15m`) against VictoriaMetrics (5m instant-query staleness lookback),
that is a ceiling of 10 minutes; an 8-minute window leaves at least 2 minutes of margin in
the worst case.

**Three preconditions, all of which must be checked against the LIVE rule, not the repo
copy, before a single sample is pushed:**

1. **Re-read `for:` from the deployed rule.** `curl /api/v1/provisioning/alert-rules` and
   confirm the value the arithmetic depends on. A rule whose `for:` is `0s` — both of
   #217's sibling rules are — has no headroom at all and cannot be probed this way.
2. **`noDataState` must not be `Alerting`.** The mechanism works by making the series
   disappear. On a rule with `noDataState: Alerting` that disappearance is itself a page.
   (#217's buffer rule is `noDataState: OK`, which is why it was safe. This precondition is
   reasoned from how the mechanism works, not separately measured.)
3. **Label the probe so it can never be mistaken for a host.** `host="synthetic-217-probe"`
   keeps it out of every real per-host series and reads as obviously synthetic in any UI or,
   in the worst case, in a notification.

Then: push once per ~60s for the bounded window, poll
`/api/prometheus/grafana/api/v1/rules` each minute, and **capture every poll** — the record
that the rule was never `firing` is the deliverable, alongside the labelled `Pending`
instance. Stop pushing on schedule; stop everything immediately if any poll shows
`Alerting`.

## Why This Matters

Measured on the live stack, 2026-08-29, against `obs-vector-buffer-filling`
(`for: 15m`, group interval 5m, `noDataState: OK`, threshold 134217728):

| moment | what happened |
| --- | --- |
| 22:24:11Z | new rule applied; API re-read confirms `for: 15m` and `max by (host, component_id) (vector_buffer_size_bytes)` |
| 22:24:41Z → 22:31:41Z | 8 pushes of `vector_buffer_size_bytes{component_id="victorialogs",host="synthetic-217-probe"} 200000000` to `/api/v1/import/prometheus`, all HTTP 204, then STOP (t=7m, inside the 8m budget) |
| 22:27:00Z | first `Pending`: one instance, `host="synthetic-217-probe"`, `activeAt=22:27:00Z` — **2.3 minutes after the first push**, not a full interval |
| 22:27:00Z → 22:41:42Z | the eight real instances (4 hosts × 2 component_ids) stayed `Normal` at every poll |
| ~22:36:41Z | last push + 5m staleness: the synthetic series leaves the instant-query lookback |
| 22:42:00Z | the evaluation at which `for: 15m` would have been satisfied — 5.3 minutes after the series was already gone |
| 22:43:46Z | `vector_buffer_size_bytes{host="synthetic-217-probe"}` returns an empty result; `ALL FIRING RULES` = `['Alert delivery heartbeat']`, the deliberate one |

Rule state was `inactive` or `pending` at all 18 polls and `firing` at none. Nothing touched
a contact point, a notification policy, or any `for:`-adjacent setting.

The line worth carrying forward is the one that was nearly assumed away: **`for:` starts
counting at the first Pending evaluation, which can arrive seconds after the first push —
not one evaluation interval later.** Sizing the push window against "the first eval is ~5
minutes in" would have quietly consumed most of the margin. Write down the falsifier before
measuring, as CLAUDE.md demands: this drill's falsifier is *any* poll showing `Alerting` or
`rule.state=firing`, or a notification arriving. None did.

**What this technique does NOT prove, and must not be claimed to.** It verifies that the
right instances exist with the right labels. It says nothing about **notification
delivery** — whether two labelled instances actually arrive as two separate messages. That
still requires real firing and real counting, which is #201's territory and #201's hazard;
this convention narrows the blast radius of the labelling question only, it does not close
#201.

## When to Apply

Whenever a change alters a rule's `by (...)` clause or the labels its summary interpolates,
**and** the rule has a `for:` comfortably longer than the datasource's staleness window and
a non-alerting `noDataState`. When either precondition fails, do not improvise a longer
window or relax a rule setting to make the drill fit — that converts a safe probe into
#201. Fall back to reading the instance list the rule already produces from real data (in
#217 that alone showed four correctly-labelled `Normal` instances where the old grouping
produced two unlabelled ones), and say plainly what was and was not proven.

## Examples

Re-read the live rule first — this is the step that makes the arithmetic real:

```bash
set -a; . /data/deploy/observability/.env; set +a
curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" -H "Host: $GRAFANA_DOMAIN" \
  http://127.0.0.1:3000/api/v1/provisioning/alert-rules   # confirm for: and noDataState
```

(The `-H "Host: ..."` is not optional: without it Grafana 301-redirects the local request to
its public URL and the response is not JSON.)

Push, bounded, and poll:

```bash
curl -s -o /dev/null -w '%{http_code}' -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  --data-binary 'vector_buffer_size_bytes{component_id="victorialogs",host="synthetic-217-probe"} 200000000' \
  http://127.0.0.1:8428/api/v1/import/prometheus          # 204

curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" -H "Host: $GRAFANA_DOMAIN" \
  http://127.0.0.1:3000/api/prometheus/grafana/api/v1/rules   # read state + alerts[].labels
```

Confirm afterwards that the series is genuinely unreachable to the rule — note that it is
still in the TSDB, it has simply left the instant-query lookback:

```bash
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  --data-urlencode 'query=vector_buffer_size_bytes{host="synthetic-217-probe"}' \
  http://127.0.0.1:8428/api/v1/query                      # empty result
```

One artefact of the run worth knowing about: a concurrent deploy replaced the rule while the
probe's instance was still listed, and the orphaned `Pending` instance remained visible in
the rules API afterwards even though the rule that produced it was gone. An instance in that
list is not proof that the rule currently provisioned produced it — check the served `expr`
alongside the instance list.

## See also

- [drill-alerts-self-identify-and-are-operator-consented.md](drill-alerts-self-identify-and-are-operator-consented.md)
  — #201, the synthetic probe that paged the operator at 3am, and the convention that
  answers it: the drill markers, the `-e observability_drill_issue` consent window, and the
  graded table this technique sits at the top of (grade A, labelling only). Two of this
  page's preconditions gained measured backing there: `isPaused: true` is **not** a
  substitute for a non-alerting `noDataState` — #212 measured that a paused rule is excluded
  from evaluation entirely, so it proves nothing about a rule that must evaluate — and the
  `noDataState` precondition, reasoned here from how the mechanism works, has a real case
  behind it now in #206's fixture, which reached `state=pending` carrying no marker at all.
- [removed-metric-did-not-go-nodata-mixed-version-fleet.md](../integration-issues/removed-metric-did-not-go-nodata-mixed-version-fleet.md)
  — #216, the dropout that motivated #217's regrouping.
- [instant-query-cannot-prove-a-series-is-live.md](instant-query-cannot-prove-a-series-is-live.md)
  — the staleness window this technique turns into a safety mechanism, read the other way round.
- [experiment-must-discriminate-between-hypotheses.md](experiment-must-discriminate-between-hypotheses.md)
  — where the "write the falsifier down before measuring" rule comes from.
- [prove-notification-delivery-not-just-config-validity.md](prove-notification-delivery-not-just-config-validity.md)
  — the counterpart discipline for the half this technique deliberately does not cover.

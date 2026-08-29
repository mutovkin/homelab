---
title: "A removed metric did not go NoData: a mixed-version fleet kept the alert looking healthy"
date: 2026-08-29
category: integration-issues
module: observability
problem_type: integration_issue
component: monitoring
symptoms:
  - "Grafana alert rule evaluates normally and returns plausible values after the metric it queries was removed upstream"
  - "noDataState never engages because other instances still export the old metric name"
  - "an upgraded instance silently drops out of a by(component_id) aggregate with no state change"
  - "Vector 0.58.0 removed buffer_byte_size and buffer_events gauges"
root_cause: version_skew
resolution_type: config_change
severity: high
related_components:
  - vector
  - grafana
  - victoriametrics
  - watchtower
tags:
  - vector
  - grafana
  - observability
  - breaking-change
  - silent-failure
  - alerting
  - version-skew
  - metric-rename
---

# A removed metric did not go NoData: a mixed-version fleet kept the alert looking healthy

## Context

Watchtower reported `timberio/vector:latest-distroless-static` (0.58.0). Vector is
`monitor-only`, so the update only happens on a deliberate deploy. 0.58.0's breaking
changes include:

> The deprecated `buffer_byte_size` and `buffer_events` gauge metrics have been removed.

`obs-vector-buffer-filling` queried `max by (component_id) (vector_buffer_byte_size)`.

## The mistake worth recording

The obvious analysis — the one written into the first commit of #216, into its GitHub
issue, and into the first version of this repo's own inline comment — was:

> The metric vanishes, the rule goes NoData, and `noDataState: OK` swallows it silently.

**That is wrong.** It is a reasonable inference, it fits this repo's existing
"absence owner" vocabulary, and it survived being typed out three times because it
*sounds* like the known failure mode. It was only caught by measuring the live fleet
after the apply.

## What actually happens

The expression aggregates `by (component_id)` with **no host label**, and this fleet is
**mixed-version by design**: only the eq12_docker *container* floats to 0.58.0, while the
three native `vector_agent` shippers are pinned at 0.57.0 in
`roles/vector_agent/defaults/main.yml`. `component_id="victorialogs"` exists on all four.

So the pinned 0.57.0 agents keep exporting the removed name, and the rule never goes
NoData. Measured at `2026-08-29T21:52:24Z`, 6.8 minutes after the 0.58.0 start — past
VictoriaMetrics' 5m staleness lookback, and independently reproduced at `21:55:59Z`:

```
vector_buffer_byte_size{component_id="victorialogs"}
  -> hosts ['eq12', 'n5pro', 'n5pro_docker']        # eq12_docker simply GONE

max by (component_id) (vector_buffer_byte_size)     # the rule's own expression
  -> 2 series [('vector_metrics_out','0'), ('victorialogs','0')]   # NOT NoData
```

The rule evaluates normally, returns plausible healthy numbers, changes no state, and
logs no error — while no longer covering the **one** instance whose 256MB disk buffer the
128MiB threshold and the wedged-buffer runbook are actually about.

**Silent partial blindness inside a healthy-looking aggregate is strictly worse than
NoData.** NoData is at least a state a rule can be configured to act on. This is a guard
that reports "fine" forever because its remaining inputs are fine.

## Why the timing nearly hid it too

The first post-upgrade check appeared to show the old name still alive on eq12_docker
with a healthy-looking `14496`. That was its **last pre-upgrade sample**, still inside
VictoriaMetrics' 5m instant-query staleness lookback. A check run in the first five
minutes after an upgrade cannot distinguish "still exporting" from "recently stopped" —
and the stale value looks live. Either wait past the staleness window or query a tight
explicit window (`last_over_time(...[90s])`), which is what actually separated them.

Related, from CLAUDE.md and re-confirmed here: VM ingestion is not immediately queryable.
The first check ran 73s after restart and both names' newest sample was still pre-restart.

## The fix

Swap to `vector_buffer_size_bytes`. Verified as a drop-in on the live 0.57.0 fleet
**before** the bump — and deliberately not by the test that would have proved nothing:
both gauges read 0 at idle, which cannot distinguish a shared gauge from two unrelated
ones. What discriminates:

| check | result |
| --- | --- |
| `max_over_time(...[30d])` | **328952 bytes** for `component_id=victorialogs` on BOTH names — same non-zero peak |
| `count(vector_buffer_byte_size != vector_buffer_size_bytes)` | empty — zero disagreeing samples in retention |
| `count by (host)` | 2 series on all **four** instances, so the new name is correct on 0.57.0 and 0.58.0 at once |

That last row is what makes the swap safe to land on a mixed-version fleet in one step.

## Lessons

1. **A metric removal in a mixed-version fleet does not produce absence — it produces a
   quieter aggregate.** `noDataState` only protects you when *every* contributor stops.
   Where instances are deliberately version-skewed (a floating container tag beside a
   pinned `.deb`), any unlabelled aggregate can lose a member silently. Filed as #217.
2. **Version skew that is documented as intentional is exactly where this bites.**
   `vector_agent/defaults/main.yml` already said the container and the agents "WILL
   drift". The drift was known; that it could disarm an alert without any signal was not.
3. **Write down what would falsify the analysis before believing it.** "It goes NoData"
   predicts an empty result. One query would have tested it, and none was run until after
   the apply — the reasoning felt conclusive because it matched a familiar pattern.
4. **Don't cite upstream for a claim you actually measured.** The first draft attributed
   0.57's emit-both behaviour to Vector's `docs/specs/buffer.md`. That spec documents the
   emit-both rule for `buffer_max_*` and says no such thing about these two gauges. The
   fleet measurement was real; the citation was not.
5. **A deprecation notice in release notes is a scheduled outage for anything that
   queries the name.** Same shape as CLAUDE.md's telegraf `inputs.exec` note: where a
   floating tag can cross a removal, the query surface is part of the upgrade's blast
   radius. Grep the alert/dashboard corpus for every removed name before deploying.

## Verification commands

```bash
# On the observability host, past the 5m staleness window after an upgrade:
set -a; . /data/deploy/observability/.env; set +a

# Which hosts still feed a given metric name (tight window beats an instant query)
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  --data-urlencode 'query=count by (host) (last_over_time(vector_buffer_size_bytes[90s]))' \
  http://127.0.0.1:8428/api/v1/query

# The rule as Grafana is really serving it, and whether any rule still names a dead metric
curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" -H "Host: $GRAFANA_DOMAIN" \
  http://127.0.0.1:3000/api/v1/provisioning/alert-rules

# The rule's own evaluation health (state / health / lastError)
curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" -H "Host: $GRAFANA_DOMAIN" \
  http://127.0.0.1:3000/api/prometheus/grafana/api/v1/rules
```

Note the `-H "Host: $GRAFANA_DOMAIN"`: without it Grafana 301-redirects the local request
to its public URL and the response is not JSON.

## See also

- [vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md) — the
  previous Vector upgrade that broke this pipeline, and why the container is `monitor-only`.
- [experiment-must-discriminate-between-hypotheses.md](../conventions/experiment-must-discriminate-between-hypotheses.md)
  — the idle-zero trap avoided here.
- #217 — per-host attribution for this rule and its two siblings, landed: all four
  vector-health rules now group `by (host, ...)`, so the dropout above would be a
  series that stops rather than a member quietly leaving a healthy-looking aggregate.

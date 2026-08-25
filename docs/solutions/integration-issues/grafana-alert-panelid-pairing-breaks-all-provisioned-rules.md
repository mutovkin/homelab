---
title: "Grafana rejects `__dashboardUid__` without `__panelId__`, and the rejection crash-loops the whole process: one annotation took all 22 alert rules down"
date: 2026-08-24
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "`docker logs grafana` carries `Failed to provision alerting` / `alert rules: invalid alert rule: both annotations __dashboardUid__ and __panelId__ must be specified`"
  - "the failure is not scoped to the offending rule: provisioning is a startup-blocking module, so ALL 22 provisioned rules are gone and Grafana never serves"
  - "ten `Starting Grafana` lines with backoff-shaped gaps and ZERO `HTTP Server Listen` lines across the whole outage window"
  - "the deploy fails at the health-endpoint gate with `Grafana never became healthy` — which reports that the process is down, never that an alert annotation is why"
  - "the offending file is valid YAML and passes ansible-lint and `ansible-playbook --syntax-check`, because nothing repo-side knows Grafana's alert-rule schema"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - grafana
  - victoriametrics
  - ansible
  - docker-compose
tags:
  - grafana
  - alerting
  - provisioning
  - deep-links
  - annotations
  - dashboard-panel-ids
  - observability
---

# A dashboard-only Grafana deep link is not representable, and the rejection takes the whole process down

## Problem

#187 rewrote the notification text of all 22 provisioned alert rules and, as the cheap
half of #181, added deep links to the six TrueNAS rules
(`ansible/roles/services/observability/files/data/grafana/provisioning/alerting/truenas-health.yaml`).
A deep link is a pair of reserved annotations, `__dashboardUid__` and `__panelId__`; Grafana
turns them into a "View panel" button on the notification. Four of the six rules point at an
obvious panel. The two liveness rules — `truenas-metrics-absent` and `truenas-poller-absent`
— report that a whole delivery path has gone dark, so the natural link is *the dashboard*,
not any one graph. They were given `__dashboardUid__` alone.

Grafana validates the two annotations as a **pair**. A dashboard-only deep link is not
representable in file provisioning: there is no "link at the dashboard, no panel" form. The
rejection is not scoped to the offending rule, and it is not scoped to alerting — file
provisioning is a startup-blocking module, so one unpaired annotation on one rule stopped
the Grafana process from ever serving.

Verified against the pre-fix state of the #187 branch: the file parses cleanly as YAML, and exactly
two of its six linked rules were unpaired.

```
truenas-hdd-temp-warning   dash=True panel=True
truenas-hdd-temp-critical  dash=True panel=True
truenas-metrics-absent     dash=True panel=False
truenas-poller-absent      dash=True panel=False
truenas-pool-degraded      dash=True panel=True
truenas-scrub-overdue      dash=True panel=True
parsed OK, rules with links: 6
```

## Symptoms

Measured on eq12_docker (192.168.25.15), Grafana 13.2.0, 2026-08-24.

- The rejection, once, at 19:48:36:

  ```
  logger=provisioning level=error msg="Failed to provision alerting"
  error="alert rules: invalid alert rule: both annotations __dashboardUid__ and __panelId__ must be specified"
  ```

- It is not a warning and it does not skip the rule. Provisioning is a module in Grafana's
  startup dependency graph, and a failed module fails the boot:

  ```
  msg="Module failed" module=provisioning err="starting module provisioning: invalid service
  state: Failed, expected: Running, failure: alert rules: invalid alert rule: both annotations
  __dashboardUid__ and __panelId__ must be specified"
  ```

  and then the dependents fall over in turn, each logged as
  `failed to start X, because it depends on module provisioning, which has failed`.
  Measured victims: `*store.standardStorageService`, `*rendering.RenderingService`,
  `*cleanup.CleanUpService`.

- **Grafana never started.** Between 19:48:36 and 19:51:23 the log carries TEN
  `msg="Starting Grafana"` lines — 19:48:44, 19:48:50, 19:48:56, 19:49:03, 19:49:11,
  19:49:22, 19:49:40, 19:50:10, 19:51:07, 19:51:23 — with restart-backoff-shaped gaps of
  6, 6, 7, 8, 11, 18, 30, 57 and 16 seconds, and **zero** `msg="HTTP Server Listen"` lines
  in that entire window. The first listen after the incident is 19:51:26, i.e. only once
  every deep link had been paired.

- So the blast radius is total, from one annotation on one rule: no UI, no API, no dashboards,
  no alert evaluation, all 22 rules gone. And the rule that lost its link was one of the two
  whose job is to announce that the NAS metrics pipeline has gone dark.

**Correct a tempting framing before it hardens.** This is *not* one of this repo's
"silent green" failures — the container was crash-looping and unreachable, and any probe of
the service says so. What is true, and is the reason it is worth a document, is that the
**cause** lives in exactly one container log line. `docker ps` tells you Grafana is down; it
does not tell you an alert annotation is the reason. And the offending annotation is
syntactically valid YAML in a role `files/` payload that the repo-side toolchain never parses
as a Grafana document (`ansible/roles/services/observability/tasks/main.yml:294`–`:318` rsyncs
`ansible/roles/services/observability/files/data/grafana/provisioning/` verbatim), so nothing local — YAML parse, ansible-lint,
`ansible-playbook --syntax-check` — has any opportunity to object.

The apply does fail loudly, at the right place, for the wrong-looking reason: the role gates
on Grafana's health endpoint after any provisioning change (`ansible/roles/services/observability/tasks/main.yml:457`–`:468`, 12
retries × 5s), so the play stops with "Grafana never became healthy". The gate's own comment
(`ansible/roles/services/observability/tasks/main.yml:449`–`:456`) already predicted this class of failure in the abstract — "a
mistyped alert rule … restarts Grafana into a crash loop" — but a gate reports *that* the
process is down, never *why*.

## What Didn't Work

- **A dashboard-only deep link.** The thing we actually wanted. Not available: `__panelId__`
  is mandatory whenever `__dashboardUid__` is present. We found no alternative spelling that
  Grafana accepts as "the whole dashboard".
- **Assuming a bad rule degrades gracefully.** It does not. Grafana's file provisioner treats
  a rejected document as a fatal startup error, and a fatal startup error in a startup-blocking
  module is a crash loop, not a skipped rule.
- **Repo-side validation.** The provisioning directory is role `files/` content, rsynced as-is. YAML
  validity, ansible-lint and `--syntax-check` all pass on a document Grafana will reject,
  because none of them knows the Grafana alert-rule schema. `--check` is worse than useless
  here: it skips the restart entirely (`when: not ansible_check_mode`,
  `ansible/roles/services/observability/tasks/main.yml:447`),
  so the dry run of the change that kills Grafana is green.
- **Reading the YAML.** (session history) The unpaired annotations survived review on the
  branch and were caught only by applying them to a live Grafana — the reason the fix landed
  as its own commit across every linked rule rather than folded into the text rewrite. The
  sibling incident 48 hours earlier had the same tell: every layer that was checked looked
  correct except the one that mattered.

## Solution

Pair every `__dashboardUid__` with a `__panelId__`, and choose the panel by what the rule
actually reports — the panel that goes blank when the rule fires.

Before (`truenas-metrics-absent`):

```yaml
          __dashboardUid__: nas-truenas
          runbook_url: https://github.com/mutovkin/homelab/blob/master/docs/solutions/…
```

After (`truenas-health.yaml:279`–`:286`):

```yaml
          __dashboardUid__: nas-truenas
          # Grafana REJECTS __dashboardUid__ without __panelId__ ("both annotations
          # ... must be specified") and refuses to start — a dashboard-only deep
          # link is not representable. 13 = ARC size and memory, the first panel
          # this rule's own description names as going dark, and Graphite-fed
          # (truenas_arcstats_*) like the stream this rule watches.
          __panelId__: "13"
          runbook_url: https://github.com/mutovkin/homelab/blob/master/docs/solutions/…
```

and `truenas-poller-absent` (`truenas-health.yaml:367`–`:371`) to panel 3, Disk temperature —
the poller-fed series (`truenas_disk_temperature_celsius`) whose blindness that rule reports.
The comment above each pairing exists so a later cleanup pass does not "tidy" the
redundant-looking line away.

The full linked set in the current tree:

| Rule | `__panelId__` | Panel title |
| --- | --- | --- |
| `truenas-hdd-temp-warning` (`:95`) | `"3"` | Disk temperature |
| `truenas-hdd-temp-critical` (`:181`) | `"3"` | Disk temperature |
| `truenas-metrics-absent` (`:279`) | `"13"` | ARC size and memory |
| `truenas-poller-absent` (`:367`) | `"3"` | Disk temperature |
| `truenas-pool-degraded` (`:441`) | `"5"` | Pool status |
| `truenas-scrub-overdue` (`:515`) | `"7"` | Days since scrub |

The second, structural half of the fix: **a panel id referenced from an alert rule must be
pinned in the dashboard JSON.** Grafana assigns ids at load when a panel has none, and an
export or a UI edit renumbers them. The #187 branch therefore wrote explicit `"id"` values
1–17 in document order — rows included, because rows consume ids too — into
`ansible/roles/services/observability/files/data/grafana/dashboards/nas/truenas.json`
(uid `nas-truenas`, title "NAS — TrueNAS"): 1 Disk health (row), 2 Hottest HDD, 3 Disk
temperature, 4 Pool (row), 5 Pool status, 6 Pool used, 7 Days since scrub, 8 Pool capacity,
9 Disk I/O (row), 10 Disk reads, 11 Disk writes, 12 ZFS ARC (row), 13 ARC size and memory,
14 ARC hit ratios, 15 System (row), 16 CPU usage, 17 Memory.

## Why This Works

Two different failure modes, closed by the two halves.

The pairing closes a **loud** one. Grafana's provisioner is a hard schema gate on a
startup-blocking module: valid pair or no process. Once every `__dashboardUid__` has a
`__panelId__` beside it the document passes and the boot completes — the proof is the
`msg="HTTP Server Listen"` line at 19:51:26, the first in the whole incident window.

The pinned ids close a **completely silent** one, which is the half that would have bitten
later. An unpinned panel id is assigned at dashboard load. Renumber the panels — by exporting
the dashboard from the UI, by inserting a panel, by reordering rows — and `__panelId__: "3"`
still validates, Grafana still starts, the notification still carries a "View panel" button,
and the button now opens the wrong graph. There is no error anywhere in that path. Pinning the
ids in the JSON we deploy makes the alert file's reference a reference to something stable
rather than to an accident of document order.

Choosing the panel by what the rule reports, rather than by convenience, is what makes the
mandatory pairing tolerable: `truenas-metrics-absent` links at the first panel its own
description names as going dark, so the forced choice carries information instead of noise.

## Corollary: an instant query cannot prove a rewritten notification was delivered

The other half of #187 was the notification *text*. Proving text reached a human means
proving a send happened, and the obvious metric has the same shape as the trap this repo
already documented in #151.

`grafana_alerting_notifications_total{integration=…}` does not exist until the first send
since process start, and it is exported only transiently around sends — measured previously
and recorded at
`ansible/roles/services/observability/files/data/telegraf/telegraf.conf:366`–`:372`: absent
from every sample over 4.5 minutes on a healthy idle instance, and present in
VictoriaMetrics for about six minutes after a notification before vanishing for the rest of a
healthy hour. So an **instant** query against it returns empty for a channel that demonstrably
delivered ten minutes ago, and empty is indistinguishable from "never sent". Query the
**range** over the test window instead.

Measured for this change, `query_range` over 19:45–20:30 on 2026-08-24:
`{integration="email"}` = 1 and `{integration="telegram"}` = 1 — both channels sent, once
each. And `grafana_alerting_notifications_failed_total` has **no series at all**, which is the
same trap seen from the other side: a counter that only comes into existence once it is
non-zero, so its absence is the healthy state and cannot be alerted on as NoData.

This is an application of two existing learnings rather than a new one — see
[instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
and
[absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md),
plus the CLAUDE.md gotcha from #151.

## Prevention

**Before shipping any deep link:**

1. Grep the pair, never one annotation. Every `__dashboardUid__` in the tree must have a
   `__panelId__` in the same `annotations:` block:

   ```bash
   git grep -n "__dashboardUid__\|__panelId__" -- ansible/roles/services/observability/
   ```

   The current tree has six real pairs, adjacent (`truenas-health.yaml:95/96`, `181/182`,
   `279/285`, `367/371`, `441/442`, `515/516` — the two wider gaps are the explanatory
   comments, and those comments are load-bearing documentation, not padding). The grep
   prints thirteen lines, not twelve: `:280` is a comment naming both tokens. Count with
   `git grep -c` per token (7 and 7, comment included) rather than eyeballing the total.

2. If the rule is about a whole pipeline going dark and no single panel fits, you still have
   to pick one. Pick the panel the rule's own `description` names first, and write a comment
   saying why — as `truenas-health.yaml:280`–`:284` and `:368`–`:370` do. "No panel" is not an
   option the format offers.

3. Pin the panel id in the dashboard JSON in the *same change* as the annotation. A
   `__panelId__` that points into a dashboard with unpinned ids is a link that will silently
   drift. When editing a dashboard through the Grafana UI and re-exporting, diff the `"id"`
   values before committing — a renumber produces no error at any layer.

**Do not expect local tooling to catch it.** The alerting YAML is role `files/` content rsynced
verbatim (`ansible/roles/services/observability/tasks/main.yml:294`–`:318`); nothing in this repo validates it against Grafana's
alert-rule schema. A YAML parse, `task ansible:lint` and `ansible-playbook --syntax-check` all
pass on a document Grafana will refuse. `--check` skips the Grafana restart entirely
(`ansible/roles/services/observability/tasks/main.yml:447`), so the dry run of a change that crash-loops Grafana is
green. The
first real test of a provisioning-file edit is the live apply — the repo's standing rule that
a task whose inputs only exist after a real apply is invisible to `--check`, applied here to a
whole file format.

**Evidence that recovery is real**, in order, on the host:

```bash
# 1. Grafana actually reached the listen stage AFTER your apply.
ssh root@192.168.25.15 'docker logs grafana --since 10m | grep -c "HTTP Server Listen"'
# must be >= 1, with a timestamp later than the apply.
# The complement is the diagnosis: grep "Failed to provision alerting" for the reason,
# and count "Starting Grafana" — repeated starts with growing gaps is the crash loop.

# 2. Every rule is loaded, not just the process up. Host header, not a redirect follow:
#    GF_SERVER_ROOT_URL/GF_SERVER_DOMAIN make Grafana 301 any request whose Host does not
#    match, so a 127.0.0.1 call without it leaves the container (same reason the datasource
#    health check sets one, in this role's tasks/main.yml).
#    The credentials live in the deploy dir's .env, NOT in root's login shell — without
#    sourcing it the single-quoted command expands them to empty and the call just 401s.
ssh root@192.168.25.15 \
  'set -a; . /data/deploy/observability/.env; set +a;
   curl -su "$GRAFANA_USER:$GRAFANA_PASSWORD" -H "Host: $GRAFANA_DOMAIN" \
     http://127.0.0.1:3000/api/v1/provisioning/alert-rules \
   | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"'
# expect 22 — the sum of the per-file rule counts under
# files/data/grafana/provisioning/alerting/ (delivery 3, ingest 3, per-host-ingest 1,
# probe 3, telegraf 2, truenas 6, vector 4).
```

The rule count is the check that distinguishes "Grafana came up" from "Grafana came up with
your rules". A partial or reverted provisioning directory produces a perfectly healthy process
serving fewer rules than the repo declares, and `/api/health` is happy with that.

**And that count is coupled by hand.** If you add or remove a rule, the 22 above is stale.
Read it from the tree rather than from memory:

```bash
grep -h "^      - uid:" \
  ansible/roles/services/observability/files/data/grafana/provisioning/alerting/*.yaml \
  | wc -l
# Scope it to that directory: the same 6-space `- uid:` shape also matches the contact-point
# uids in the templated contact-points file, which are not alert rules.
```

## Related Issues

- **#187** — the notification rewrite this was found during (all 22 rule descriptions cut
  from 43–475 words to 12–50 — the issue title's "120-475" was the median-ish impression,
  and six rules were already under 120); **#181** — the issue that proposed deep links in the first
  place. This branch closed #181's cheap half (deep links on the six TrueNAS rules); the
  grafana-image-renderer evaluation and deep links for the non-NAS rules remain open there.
- **#176** — added `truenas-metrics-absent`; **#174** added `truenas-poller-absent`. Those
  are the two liveness rules whose "link at the whole dashboard" intent hit this wall, and
  they are separate rules precisely because they watch two separate delivery paths.
- [grafana-datasource-version-gate-freezes-rotated-secret](grafana-datasource-version-gate-freezes-rotated-secret.md)
  — the sibling, found 48 hours earlier in the same subsystem: a *different* mechanism
  (provisioned object frozen behind a `version:` gate) with the same lesson that Grafana
  provisioning has failure modes no repo-side check can see. Keep the two distinct; the
  root causes are unrelated.
- [grafana-alerting-provisioned-but-undeliverable](grafana-alerting-provisioned-but-undeliverable.md)
  — same file, same module, the #139 write-up of constructs that are valid in isolation and
  wrong in composition.
- [instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
  and [absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md)
  — the two docs the delivery-proof corollary above applies rather than restates (#151, #152).

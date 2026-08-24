---
title: "Telegraf's host label: fabricated by the agent, then stripped again by a per-plugin taginclude"
date: 2026-08-23
category: integration-issues
module: services/observability
problem_type: integration_issue
component: tooling
symptoms:
  - "every telegraf metric carries host=homelab-telegraf, a name matching no machine in the fleet"
  - "docker_* series carry container_name and container_image and no host, environment or location at all"
  - "count by (host) over those series returns one plausible-looking row whose label set is EMPTY"
  - "telegraf --test with the env var absent emits host=${TELEGRAF_HOSTNAME}, literally, and exits 0"
  - "a new per-host lag absence rule arrives FIRING and stays that way for a full 24h lookbehind"
root_cause: config_error
resolution_type: config_change
severity: medium
related_components:
  - telegraf
  - victoriametrics
  - grafana
  - docker
  - ansible
tags:
  - telegraf
  - host-label
  - taginclude
  - global-tags
  - env-var
  - victoriametrics
  - alerting
  - silent-green
---

# Telegraf's `host` label, and the two places it goes missing

Landing #178 (make every metric answer "which machine is this from?" the same
way: `host` = Ansible `inventory_hostname`, plus `host="truenas"` for the
appliance). The headline fix was one line. Everything below is what the fix
walked into, all of it measured on eq12_docker rather than reasoned about.

This is the **metrics** twin of
[vector-hostname-and-severity-labels-were-fabricated](vector-hostname-and-severity-labels-were-fabricated.md)
(#143, the logs side): same defect shape — an identity label derived inside the
container instead of from the inventory — and the same fix shape
(`inventory_hostname` → `env.j2` → `${VAR:?}` in compose). It is also the
fleet-wide completion of section 3 of
[truenas-26-api-exporter-configured-is-not-delivering](truenas-26-api-exporter-configured-is-not-delivering.md)
(#173/#174), which fixed the same defect for the TrueNAS stream only.

## Problem

`telegraf.conf`'s `[agent]` block pinned `hostname = "homelab-telegraf"`, so
every metric telegraf produced — cpu, mem, disk, net, netstat, system, docker,
ping, http_response and the Grafana `/metrics` scrape — was attributed to a
collector that is not a machine. Replacing it with an env-substituted value fixed
the agent-tagged families immediately and revealed two further problems that the
fabricated name had been hiding.

## Symptoms

- `host="homelab-telegraf"` on 162 metric names, matching no host in the inventory.
- `docker_*` series (448 of them) with **no** `host`, `environment` or `location`
  tag — before *and* after the agent fix.
- `count by (host) ({__name__=~"docker_.+"})` returning
  `[{"metric":{},"value":[...,"448"]}]` — one row, empty label set, no error.
- `telegraf --test` with `TELEGRAF_HOSTNAME` absent emitting
  `host=${TELEGRAF_HOSTNAME}` as a literal string, exit 0, agent healthy.

## What didn't work

**Reading the agent setting as the whole fix.** Setting
`hostname = "${TELEGRAF_HOSTNAME}"` made cpu/mem/disk/net/ping/http_response and
the Grafana scrape correct within one collection interval, and a
`count by (host)` over those families returned exactly one row,
`host="eq12_docker"`. The same query over `docker_*` also returned exactly one
row — and it looked like a pass. It was not: the row's label set was empty.

**Assuming telegraf 1.38+ "strict environment variable handling" catches an
unset variable.** It does not. It is printed as a startup warning on every boot,
which makes it easy to assume it covers this; measured, it does not (see below).

## Root cause

### 1. `taginclude` is an allowlist over the FINAL tag set

`[[inputs.docker]]` carried, under a comment about promoting string fields to
tags for Prometheus compatibility:

```toml
taginclude = ["container_id", "container_name", "container_image"]
```

`taginclude` does not select which *string fields* become tags. It filters the
finished tag set, and it runs **after** the agent has already added `host` and
everything in `[global_tags]`. So the allowlist was silently deleting the host
dimension from every `docker_*` series — for the whole life of the deployment,
with no error, a healthy container and a green deploy every time.

**The tell that it was the filter and not a missing tag: `environment` and
`location` were missing too — i.e. exactly the `[global_tags]` set.** A tag that
was never added and a tag that was added and then filtered look identical from
the query side; the global tags are the control group that separates them.

Fix — one entry, and it must stay:

```toml
taginclude = ["container_id", "container_name", "container_image", "host"]
```

`environment`/`location` are deliberately left out — but **not** for cardinality,
which was the wrong word for it. Both are fleet-constant, and adding a *constant*
label to existing series creates no new series: the count stays 448 either way.
The real cost is **width** — one more name/value pair carried in every series'
identity, in the index and in every remote-write payload — for a dimension that
can only ever take one value on this fleet.

The flip side, recorded because it is a deliberate asymmetry rather than an
oversight: `taginclude` appears only under `[[inputs.docker]]`, so `docker_*` is
the one telegraf family that does **not** carry `environment`/`location`. A
future cross-family selector — `{environment="homelab"}`, a dashboard variable
built on `location` — silently misses every `docker_*` series. It still returns
rows, just not those ones, which is the same shape of quiet wrongness this whole
document is about. Adding a host to the stack, or a selector like that, means
adding both tags to the allowlist rather than assuming they are already there.

Why `host` specifically is not cosmetic: `container_name` is **not** unique
across machines. The day this stack runs on a second host, both hosts'
per-container series become label-identical and merge silently — the same
collision shape as the graphite measurement names in
[truenas-26-api-exporter-configured-is-not-delivering](truenas-26-api-exporter-configured-is-not-delivering.md).

### 2. An unset env var is not a config error

Measured with `telegraf --test` against the running image (1.39.3), the
deployed config bind-mounted read-only, one variable changed per run:

| `TELEGRAF_HOSTNAME` in the container env | resulting tag | exit |
| ---------------------------------------- | ------------- | ---- |
| `eq12_docker` | `host=eq12_docker` | 0 |
| empty string | `host=43dc22b498df` — `os.Hostname()`, a 12-char container id that changes on every recreate | 0 |
| **absent** | `host=${TELEGRAF_HOSTNAME}` — **the literal string** | 0 |

The third row is the dangerous one, and it is the reachable one: delete the
passthrough line from `compose.yaml` while `telegraf.conf` still references the
variable, and every metric on the host gets a wrong-but-present label with the
agent healthy and every deploy green. Compose's `${VAR:?}` guard covers the
`.env` → compose hop only; nothing covers the compose → container hop.

Contrast, in the same stack: Vector 0.57 disables `${VAR}` interpolation in its
config by default and needs
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION` — see
[vector-interpolates-env-vars-inside-comments](vector-interpolates-env-vars-inside-comments.md).
Two collectors, one compose file, opposite env-var strictness.

### 3. Nothing could have noticed either one

Every other telegraf-fed rule aggregates the host dimension **away**:
`probe-health` groups `by (server, check_type)`, `delivery-health` by
`integration`. A wrong-but-present `host` moves none of them. The label had no
absence owner at all, which is the general rule in
[absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md).

## Solution

1. `hostname = "${TELEGRAF_HOSTNAME}"` in `[agent]`, fed by
   `TELEGRAF_HOSTNAME='{{ inventory_hostname }}'` in the role's `env.j2` and a
   `:?`-guarded passthrough in `compose.yaml` — the `VECTOR_HOSTNAME` mechanism.
2. `"host"` added to `[[inputs.docker]]`'s `taginclude`.
3. A new absence owner —
   `ansible/roles/services/observability/files/data/grafana/provisioning/alerting/telegraf-health.yaml`,
   rule `obs-telegraf-metrics-absent` — so the label has a durable control rather
   than a once-per-deploy assert.

Historical series keep `host="homelab-telegraf"` (label boundary 2026-08-23).
Nothing consumed the old value — audited across every dashboard and alert `expr`
— so the break was accepted rather than shimmed.

## The two verification traps met on the way

### `count by (label)` cannot tell "one host" from "no label"

Over series that lack the label entirely, `count by (host) (...)` returns **one
row with an empty `metric` object** — not an error, not zero rows:

```json
{"metric":{},"value":[1787541716,"448"]}
```

Next to a healthy result it reads as a pass, and only the empty `{}`
distinguishes them. Pair every `count by (<label>)` check with an unfiltered
series lookup that shows the real label set:

```bash
curl -s -u "$U:$P" --data-urlencode 'match[]={__name__="docker_container_mem_usage"}' \
  http://victoriametrics:8428/api/v1/series
```

This is the label-presence axis of
[instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
and another instance of
[verification-instrument-must-distinguish-fixed-from-broken](../conventions/verification-instrument-must-distinguish-fixed-from-broken.md).

### A per-host lag rule arrives FIRING after a deliberate rename

The obvious mirror of `obs-vector-metrics-absent` is host-agnostic:

```promql
min by (host) (lag(system_uptime[24h])) > 600
```

Run against live VictoriaMetrics *before* landing it, that form was already
breaching:

```
{host="eq12_docker"}      = 0.641 s      (healthy)
{host="homelab-telegraf"} = 1321.641 s   (+22 min after the cutover)
{host="homelab-telegraf"} = 2229.188 s   (+37 min, still climbing)
```

`lag()` returns every series with at least one sample inside the lookbehind, so
the **retired** label stays in the result set with a monotonically climbing lag
for a full 24h — the rule would have arrived firing at its own deploy and paged
for a day. A per-host lag rule pages for a full lookbehind after *any* deliberate
rename; when the rule lands in the **same change** as the rename, scope it to the
expected value instead:

```promql
min by (host) (lag(system_uptime{host="eq12_docker"}[24h])) > 600
```

That also catches the failure the rule exists for more directly — a
wrong-but-present label makes the expected series stop, which is what fires —
instead of catching it via the abandoned series, which is the very mechanism that
produced the transient. Precedent for a pinned host in a static alert file:
`truenas-health.yaml`'s `host="truenas"`. **Stated cost:** the rule then watches
exactly one name, so a second host is unmonitored until it is added to the
selector. That is a silent gap, and it belongs in the rule's own comment.

## Prevention

- **Any `*include`/`*exclude` filter on a telegraf plugin is a filter over the
  finished metric, not a promotion list.** After adding one, check that the
  `[global_tags]` still arrive — they are the cheapest control group for "did my
  filter eat the agent's tags?".
- **Verify a label fix with the label's own presence, not with a `by` clause.**
  `count by (x)` over series lacking `x` is a passing-looking answer.
- **Measure the unset/empty cases of any `${VAR}` a config depends on**, on the
  running image. For telegraf, unset ≠ error.
- **A label with no absence owner has no owner.** If every rule aggregates a
  dimension away, that dimension can rot indefinitely; give it one continuously
  exported sentinel series and one rule.
- **Before landing a per-host absence rule, run its exact query first.** If a
  retired label is still inside the lookbehind, the rule arrives firing.

## Footnote: Grafana's API 301s inside its own container

`curl http://127.0.0.1:3000/api/...` from inside the grafana container returns
`Moved Permanently` (to the public `root_url` domain) unless the request carries
`-H "Host: <domain>"`. Without it the output is empty — and "no rules firing"
and "the request never reached the API" then look identical. Already documented
in
[grafana-datasource-version-gate-freezes-rotated-secret](grafana-datasource-version-gate-freezes-rotated-secret.md);
repeated here because it silently produced an empty rule-state listing during
this change's verification.

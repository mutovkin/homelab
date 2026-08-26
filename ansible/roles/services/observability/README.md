# Observability Stack

Metrics, logs and dashboards for the homelab, on the VictoriaMetrics ecosystem.
Five containers in one compose project, deployed by Ansible onto **eq12_docker only**.

**Everything here is deployed by the pipeline.** There are no `scp`, `chown` or
`docker compose -f …` steps: the role owns the config files, the deploy dir and the
`.env`, and an ad-hoc change on the host is overwritten by the next run (Critical
Rule 1 in [CLAUDE.md](../../../../CLAUDE.md)). The instructions this file used to
carry did the opposite and are gone.

## Why this stack

- **VictoriaMetrics** (metrics) — Apache 2.0, ~10-20x better compression than
  InfluxDB, single binary, PromQL/MetricsQL. 5-year retention. It also runs a
  native InfluxDB line-protocol listener on `:8089` — **nothing writes to it**;
  see [Home Assistant integration](#home-assistant-integration).
- **VictoriaLogs** (logs) — Apache 2.0, LogsQL, far lighter than Loki. 90-day
  retention.
- **Vector** (log shipper), **Telegraf** (metrics collector), **Grafana** (UI).

Chosen over InfluxDB 3.x (proprietary) and Loki (heavier, weaker full-text search).

## Components and ports

| Service | Published | Auth | Notes |
| ------- | --------- | ---- | ----- |
| victoriametrics | 8428/tcp, **8089/tcp+udp** | `--httpAuth.*` on 8428 only | 8089 = InfluxDB ingest, see [Firewall](#firewall-8089) |
| victorialogs | 9428/tcp | `--httpAuth.*` | scratch image — no healthcheck is possible |
| vector | none | — | reads `/var/log` + the docker container log dir |
| telegraf | none | — | writes to VM, holds the docker socket |
| grafana | 3000/tcp | admin user/password | provisioned datasources, dashboards, alerts |

Full port map and conflicts: **[PORT_REFERENCE.md](PORT_REFERENCE.md)**.

The four VM/VL credentials are **mandatory** — an empty `--httpAuth` value means
"auth disabled", not "no password", so `tasks/main.yml` asserts all six credential
vars non-empty before anything is touched (#88), `env.j2` fails the template, and
compose's `${VAR:?}` is the last backstop.

`DOCKER_GID` (telegraf's supplementary group for the docker socket) is **derived at
deploy time** by stat'ing `/var/run/docker.sock` on the target host — 996 on
deb-docker, 990 on n5pro-docker. It is deliberately never defaulted: the old `999`
fallback existed on neither host, so it could only ever hand telegraf a group that
cannot read the socket, silently costing every docker metric (#94). `env.j2` fails
if the stat is undefined and compose uses `${DOCKER_GID:?}`.

## Repo file map

```
roles/services/observability/
├── tasks/main.yml                  firewall → rsyslog stream → data dirs → configs → deploy → restarts
├── tasks/vector-buffer-reset.yml   opt-in, destructive; tagged `never` (see below)
├── templates/env.j2                the .env, templated from vault + host_vars
├── files/compose.yaml              the five services
└── files/data/                     bind-mounted config, rsync'd to {{ data_mount }}
    ├── vector/vector.yaml
    ├── telegraf/telegraf.conf
    └── grafana/
        ├── config/grafana.ini
        ├── dashboards/             hot-reloaded by Grafana, no restart needed
        └── provisioning/
            ├── datasources/datasources.yaml   uids pinned — alerts reference them
            └── alerting/
                ├── ingest-health.yaml          three ingest-stalled rules (#108/#143/#154)
                ├── per-host-ingest-health.yaml per-host shipper rule (#134)
                ├── probe-health.yaml           three http_response rules (#139)
                ├── vector-health.yaml          four Vector internal-metric rules (#151)
                ├── delivery-health.yaml        heartbeat + delivery-failure (#152)
                └── truenas-health.yaml         NAS disk thermals + stream liveness (#176)
                    (contact-points.yaml AND notification-policies.yaml are
                     TEMPLATED, not files here — see templates/*.j2. Both are
                     --exclude'd from the provisioning rsync; see Alerting.)
```

The rsyslog drop-in and its logrotate policy used to live in `files/` here. #134
moved them to the shared **[`roles/rsyslog_structured`](../../rsyslog_structured/README.md)**,
which this role now `include_role`s (before the Vector config copy, as before).
They are host-log plumbing and all four hosts need the identical stream.

## Deploy

```bash
task deploy:service -- --tags observability            # both hosts (only eq12 runs it)
task deploy:service -- --tags observability --limit eq12_docker
task deploy:services -- --check --diff --limit eq12_docker   # dry run first
```

`compose.yaml` bind-mounts config directly from `{{ data_mount }}`, not from the
deploy dir, so the role copies each file there itself. Compose does **not** track
bind-mount *contents*, so a config edit alone would restart nothing — the role pairs
each copy with an explicit restart of the affected container, and does it after the
stack exists so a first deploy works too.

**Config-change flow:** edit the file in this repo → deploy → the role syncs it and
restarts only the containers whose config changed. Grafana *dashboards* are the
exception: Grafana hot-reloads them, so they sync without a restart.

## Data paths and ownership

Everything lives under `{{ data_mount }}` (`/data` on eq12_docker) and survives
container recreation:

| Path | Owner | Contents |
| ---- | ----- | -------- |
| `/data/victoriametrics` | container | the TSDB (5y of metrics) |
| `/data/victorialogs` | container | the log store (90d) |
| `/data/vector` | **root** | `vector.yaml` + `data/` (disk buffer + checkpoints) |
| `/data/telegraf` | root | `telegraf.conf` |
| `/data/grafana` | 472 (grafana) | `data/` (grafana.db), config, dashboards, provisioning |

> **NEVER `chown -R` `/data/vector`.** The buffer under `/data/vector/data` is
> written by the container as **root** and is live state, not scaffolding. The role
> creates these directories and deliberately does not manage their ownership
> recursively; a recursive chown (the old README told you to run one) corrupts the
> buffer's permissions and takes the log pipeline down silently (#109).

## Recovering a wedged Vector buffer

Vector can stop shipping while its container stays `Up` and every deploy stays green
— a full or corrupt disk buffer, or a 401 after an upstream change. Symptoms and the
full runbook:
[docs/solutions/integration-issues/vector-wedged-disk-buffer-reset.md](../../../../docs/solutions/integration-issues/vector-wedged-disk-buffer-reset.md).

The reset is a normal Ansible task, opt-in behind a tag so no ordinary run can
trigger it:

```bash
cd ansible
ansible-playbook playbooks/deploy-services.yml \
  --limit eq12_docker --tags vector_buffer_reset
```

It stops Vector, deletes the **contents** of `/data/vector/data` (never the
directory — it is a bind-mount target), starts Vector, and asserts it is running.
Queued events and the file-source checkpoints are lost; that is the price.

## Alerting

**Twenty-two** provisioned rules (16 `obs-*` + 6 `truenas-*`), all in the
**Observability** folder — count the uids in
`files/data/grafana/provisioning/alerting/` before trusting that number. It has
drifted twice already, each time in the same way: a rule was added and the
spelled-out total was not bumped, so the prose disagreed with the folder. Every
count on this page is a snapshot of that directory, not a fact this file can
enforce — adding a rule means editing the number AND the table in the same change.

#151, #152 and #154 added eight of them (four Vector-health, one docker-ingest
twin, three delivery-path); #174/#176 added the six TrueNAS rules; #178 added
**two**, and the pair is the point. The `host` label reaches VictoriaMetrics by
two INDEPENDENT delivery paths, so a detector on one is blind to the other:
`obs-telegraf-metrics-absent` watches the AGENT path (telegraf's `[agent]
hostname` stops resolving, so the expected series stops) and is the mirror of
`obs-vector-metrics-absent` for the METRICS path, while
`obs-docker-metrics-unlabelled` watches the PER-PLUGIN FILTER path (`taginclude`
strips `host` off every `docker_*` series while `system_uptime` keeps flowing,
perfectly labelled — the state that existed for the whole life of the
deployment). Every other telegraf-fed rule here aggregates the `host` dimension
away, so nothing else can see telegraf alive but stamping the wrong host.

The table below itemises **all twenty-eight**. It is what someone consults to
answer "does this signal already have an absence owner?", which is exactly the
question #178 got wrong once — so a rule missing from it is worse than a wrong
total. Routing is no longer "all by email" — see
[Notification channel](#notification-channel).

**Absence ownership, so the table is not read as covering more than it does.**
Absence is deliberately owned exactly once per signal. Since #189 every family
telegraf collects has an owner; the table records which rule, because "is this
already covered?" is the question #178 got wrong once.

| signal | absence owner |
| ------ | ------------- |
| `system_uptime` (telegraf AGENT stream) | `obs-telegraf-metrics-absent` |
| `http_response` (probe stream) | `obs-http-probe-absent` |
| `vector_uptime_seconds` | `obs-vector-metrics-absent` |
| `docker_*` | `obs-docker-metrics-absent` (#189) |
| `ping_*` | `obs-ping-metrics-absent` (#189) — anchored on `ping_percent_packet_loss`, see below |

`obs-docker-metrics-unlabelled` is **not** the absence owner for `docker_*` — it
catches those series arriving *mislabelled*, and its `noDataState: OK` is correct
precisely because its signal only exists while something is broken. Absence is a
different fault and `obs-docker-metrics-absent` owns it. Per-input death with a
healthy agent is measured here, not hypothetical — `no_new_privs` once stripped
the file capability off `/usr/bin/ping` and eight ping metrics went NO DATA while
the container stayed healthy.

**The ping anchor is the whole design of that rule, and the obvious field is
wrong.** `[[inputs.ping]]` emits nine fields and they do not disappear together:

| field | plugin cannot run | genuine 100% packet loss |
| ----- | ----------------- | ------------------------ |
| `ping_result_code` | **present**, pinned at 2 | **present**, 1 |
| `ping_percent_packet_loss` | absent | **present**, 100 |
| `ping_average_response_ms` and the four other timing fields | absent | absent |

Both columns are measured, not argued. The left one twice over: the #95 incident
recorded beside telegraf's `security_opt` block in `files/compose.yaml`, and a
53-sample window in VictoriaMetrics itself where the retired
`host="homelab-telegraf"` series carried 7391 `result_code` samples against 7338
for the rest. The right one from a disposable `telegraf --test` against
`192.0.2.1` (RFC5737 TEST-NET-1) on 2026-08-25, with `8.8.8.8` in the same run as
a positive control that produced all nine fields.

So an absence rule on `ping_result_code` is a guard that can never fire, and one
on `ping_average_response_ms` would page on every ISP blip while being unable to
tell that apart from a dead plugin. `ping_percent_packet_loss` is the only field
that survives a real outage and disappears only when the plugin cannot execute.
Do not "simplify" that selector — a `ping_.+` regex has the same defect, because
`result_code` matches it.

| uid | rule | fires when | noData |
| --- | ---- | ---------- | ------ |
| `obs-log-ingest-stalled` | VictoriaLogs ingest stalled | `_time:5m \| count()` < 1, `for: 10m` | **Alerting** |
| `obs-host-log-ingest-stalled` | Host log ingest stalled (fleet-wide) | `_time:10m source:host \| count()` < 1, `for: 10m` | **Alerting** |
| `obs-docker-log-ingest-stalled` | Docker log ingest stalled (#154) | `_time:10m source:docker \| count()` < 1, `for: 10m` | **Alerting** |
| `obs-host-log-ingest-stalled-per-host` | A host stopped shipping logs | `_time:24h source:host "homelab-heartbeat" \| stats by (hostname) count() if (_time:20m)` < 1, `for: 5m` | OK |
| `obs-http-probe-failing` | HTTP probe failing | `max by (server, check_type) (http_response_result_code)` > 0, `for: 5m` | OK |
| `obs-http-probe-bad-status` | HTTP probe returning a bad status | `max by (server, check_type) (http_response_http_response_code)` outside 200-399, `for: 5m` | OK |
| `obs-http-probe-absent` | HTTP probe stopped reporting | `min by (server, check_type) (lag(http_response_result_code[24h]))` > 600, `for: 5m` | **Alerting** |
| `obs-vector-discarding-events` | Vector is discarding events (#151) | `sum by (component_id) (increase(vector_component_discarded_events_total[15m]))` > 0, `for: 0s` | OK |
| `obs-vector-component-errors` | Vector component errors (#151) | `sum by (component_id) (increase(vector_component_errors_total[15m]))` > 0, `for: 0s` | OK |
| `obs-vector-metrics-absent` | Vector metrics export stopped (#151, #160) | `min by (host) (lag(vector_uptime_seconds[24h]))` > 600, `for: 5m` | **Alerting** |
| `obs-vector-buffer-filling` | Vector disk buffer filling (#151) | `max by (component_id) (vector_buffer_byte_size)` > 128MiB, `for: 15m` | OK |
| `obs-telegraf-metrics-absent` | Telegraf metrics stopped arriving for eq12_docker (#178) | `min by (host) (lag(system_uptime{host="eq12_docker"}[24h]))` > 600, `for: 5m` | **Alerting** |
| `obs-docker-metrics-unlabelled` | Docker metrics have lost their host label (#178) | `count({__name__=~"docker_.+", host=""})` > 0, `for: 5m` | OK |
| `obs-docker-metrics-absent` | Docker metrics stopped arriving for eq12_docker (#189) | `count by (host) (last_over_time(docker_n_containers{host="eq12_docker"}[10m]))` < 1, `for: 5m` | **Alerting** |
| `obs-ping-metrics-absent` | Ping metrics stopped arriving for eq12_docker (#189) | `count by (host) (last_over_time(ping_percent_packet_loss{host="eq12_docker"}[10m]))` < 2, `for: 5m` | **Alerting** |
| `obs-alert-delivery-heartbeat` | Alert delivery heartbeat (#152) | `vector(1)` > 0 — **always**, by design | **Alerting** |
| `obs-alert-delivery-failing` | Alert notification delivery failing (#152) | `sum by (integration) (increase(grafana_alerting_notifications_failed_total[15m]))` > 0, `for: 0s` | OK |
| `obs-alert-delivery-telemetry-absent` | Alert delivery telemetry stopped (#152) | `min(lag(grafana_alerting_alertmanager_receivers[24h]))` > 600, `for: 5m` | **Alerting** |
| `truenas-hdd-temp-warning` | TrueNAS HDD temperature above 47C (#176, repointed #174) | `max by (devname) (last_over_time(truenas_disk_temperature_celsius{host="truenas", media="hdd"}[10m]))` > 47, `for: 0s` | OK |
| `truenas-hdd-temp-critical` | TrueNAS HDD temperature above 52C (#176, repointed #174) | `max by (devname) (last_over_time(truenas_disk_temperature_celsius{host="truenas", media="hdd"}[10m]))` > 52, `for: 0s` | OK |
| `truenas-metrics-absent` | TrueNAS graphite stream stopped (#176) | `count(last_over_time(truenas_arcstats_size_size{host="truenas"}[10m]))` < 1, `for: 10m` | **Alerting** |
| `truenas-poller-absent` | TrueNAS API poller has stopped (#174) | `count(last_over_time(truenas_poller_up{host="truenas"}[10m]))` < 1, `for: 10m` | **Alerting** |
| `truenas-pool-degraded` | TrueNAS pool is not healthy (#174) | `min by (pool) (truenas_pool_healthy{host="truenas"})` < 1, `for: 0s` | OK |
| `truenas-scrub-overdue` | TrueNAS pool scrub is overdue (#174) | `max by (pool) (truenas_pool_scrub_age_seconds{host="truenas"})` > 3024000 (35d), `for: 1h` | OK |

`execErrState: Alerting` on all twenty-eight — a datasource that cannot be reached
is not evidence of health. Counted, not assumed: 28 uids and 28
`execErrState: Alerting` lines across the seven files in `alerting/`, with no
other value present.

### Why so many of the new rules are `noDataState: OK` (#151, #152)

Not a uniform default, and not laziness. Two reasons, and the second is the
non-obvious one.

**Absence must be owned exactly once**, or one dead component pages three times.
`obs-vector-metrics-absent` owns absence for the whole `vector_*` family;
`obs-alert-delivery-heartbeat` owns it for the delivery path.

Since #160 that rule groups **`by (host)`**, and that is not cosmetic labelling: a
bare `min` collapses every exporter into ONE value, so the healthiest host sets the
result and a single quiet agent is masked by its peers. That was correct while the
container was the only exporter; with four it would report Normal while one was
dead.

And **a Prometheus counter does not exist until something increments it**. Measured
on the live Grafana before these rules were written:
`grafana_alerting_notifications_total{integration="email"}` is present,
`grafana_alerting_notifications_failed_total` is **not present at all** — because
nothing has ever failed to send. Same for Vector's discard and error counters on a
healthy pipeline. `noDataState: Alerting` on those would page continuously, in
perfect health, from the moment the file lands. That is the same false-alarm shape
#134 shipped twice, and it is why the two Vector counter rules and
`obs-alert-delivery-failing` are OK on NoData while the absence owners are not.

### Vector's own health (#151)

Everything else in this stack detects only **total** absence. The ingest-stalled
rules count rows; the role's deploy-time assert waits for one marker to come back
out of VictoriaLogs. All of them are satisfied by a pipeline that is dropping half
its events — a remap abort, a stalled sink, a sink rejecting a subset. #143 replaced
Vector's fallible `!` coercions with defaulted forms precisely because an abort
drops the event with no dead-letter and no counter; that mitigation was blind by
construction until these metrics existed.

`vector.yaml` now carries an `internal_metrics` source (60s — these land in a
5y-retention TSDB, per-second scrapes of hundreds of series buy nothing) and a
`prometheus_remote_write` sink to the same VictoriaMetrics endpoint telegraf
already writes to.

**Fleet-wide since #160.** #151 scoped this to the container, so these four rules
covered `eq12_docker` and nothing else. `roles/vector_agent` now renders the same
source and sink on `eq12`, `n5pro` and `n5pro_docker`, which write to `:8428` over
the LAN — so all four hosts are covered and the rules needed no change beyond the
`by (host)` above (they already aggregate `by (component_id)` with no host
selector). Two things travel with it: `vault_vm_auth_*` moved to
`group_vars/all/vault.yml` (host_vars are invisible to other hosts) and the three
agent IPs were added to the `:8428` nftables allowlist — without that grant the
writes are dropped at the firewall with no application-level error. The sink sets
`healthcheck.enabled: false` because VictoriaMetrics answers the remote-write path
with `204 No Content` while Vector's probe expects `200`, which logged a
`Healthcheck failed` ERROR on every start while the sink worked perfectly.

**Which counter covers what is not the obvious split, and it was measured on
0.57.0 rather than assumed** (both counters are absent in health, so they were
forced on a throwaway container):

| counter | covers |
| ------- | ------ |
| `vector_component_errors_total` | sink errors including **auth failures** — the exact signature of the ~30-day silent 401 ([vector-057](../../../../docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md)) — **and VRL remap aborts** (`error_type=conversion_failed`, `stage=processing`) |
| `vector_component_discarded_events_total` | the #153 throttle engaging (`component_id=throttle_vector_own`, `intentional=true`), or a sink dropping a batch. **Not** a full buffer — this sink is `when_full: block`, so a full buffer back-pressures and stalls the sources instead of discarding; only `drop_newest` discards, and nothing here uses it |

The surprise is in the first row: **a remap abort does NOT increment the
discarded-events counter.** An aborted remap drops the event with no dead-letter
(`drop_on_abort` defaults true, nothing is rerouted), and `component_errors_total`
is the only counter that sees it — so `obs-vector-component-errors`, not
`obs-vector-discarding-events`, is what covers the silent-abort hazard #143 left
behind. Do not read a quiet discard counter as proof nothing is being dropped.

The discard series also carries an `intentional` tag — true for a throttle, false
for real loss — which answers "is this the flood bound or is this data loss?"
faster than any log.

Two couplings that nothing enforces, so both ends say so:

- The `vector_` prefix is Vector's default namespace for that sink. Vector has
  renamed internal metrics across releases (#151 says so), so a version bump means
  **re-checking** the four names in `vector-health.yaml` against
  `/api/v1/label/__name__/values`, not assuming them.
- `obs-vector-buffer-filling`'s threshold `134217728` is exactly **128 MiB**,
  i.e. half of a true 256 MiB, against `vector.yaml`'s `buffer.max_size` of
  `268435488` — which is *not* 256 MiB but 16 bytes over it, so the threshold is
  ~49.99999%, not 50%. Nothing turns on the rounding and the live `max_size` is
  deliberately left alone (changing it would resize the on-disk buffer), but the
  two numbers are coupled by hand: change both together.

### The two-rule split for log ingest, and why the per-host query looks odd (#134)

`obs-host-log-ingest-stalled` counts host records **across the whole fleet**.
eq12_docker alone contributes on the order of a thousand records per fifteen
minutes, so a hypervisor's shipper can die completely and that rule stays Normal
forever. That is why `obs-host-log-ingest-stalled-per-host` exists: the fleet-wide
rule owns **total** absence, the per-host rule owns **partial** absence.

Hence the `noData` column: the per-host rule is `noDataState: OK`, because absence
is owned by exactly one rule and one dead VictoriaLogs must not fire both.

**The naive per-host query cannot work**, and this is the same defect review caught
in #139's `[1h]` lookbehind. Written the obvious way —
`_time:15m source:host | stats by (hostname) count()` — a host that stopped
shipping produces **no row at all**, so its alert instance vanishes, Grafana marks
it stale and *resolves* the alert. It would fire for one evaluation and then
declare the dead host healthy.

The working form keeps a 24 h outer window and counts recency inside it:

```
_time:24h source:host | stats by (hostname) count() if (_time:20m) as recent
```

A dead shipper is still in the 24 h window, so it returns a row with `recent = 0` —
which a `< 1` threshold can fire on. Measured 2026-08-19 against the live
VictoriaLogs, running that shape and then the naive form as a control. The **pair**
is the proof; neither line means much on its own:

```
Q1 — the deployed shape, measured here with a 15m inner clause (the deployed expr
     uses 20m; the point of the control is the OUTER window, not the inner)
_time:24h source:host | stats by (hostname) count() if (_time:15m) as recent
  {"hostname":"d7d7d4e8c59e","recent":"0"}    <- stopped, and STILL RETURNED
  {"hostname":"eq12_docker","recent":"116"}   <- alive

Q3 — the naive form, as a control
_time:15m source:host | stats by (hostname) count() as rows
  {"hostname":"eq12_docker","rows":"116"}     <- the dead host is simply GONE
```

Q1 emits the zero-count group; Q3 drops the series entirely. That difference is the
whole reason the 24 h outer window is load-bearing.

The `victoriametrics-logs-datasource` plugin returns this as a **multi-series**
frame — one frame per hostname, carrying `hostname` as a field label — so Grafana
produces one alert instance per host and the notification names which one.
Confirmed empirically through `POST /api/ds/query`, not read off a doc page.

**Adding a host to the fleet needs no rule edit.** There is deliberately no
`hostname:` matcher, the same property #139 established for the probe rules.

The 24 h window cuts both ways, and both sides are real:

- **A retired hostname keeps alerting for up to 24 h.** The mirror of
  `obs-http-probe-absent`'s cost, accepted for the same reason — a shipper dead
  longer than a day has already paged and been ignored, and `7d` would be alert
  fatigue after an intentional decommission.
- **A host with no rows in the window is invisible, and that is the dangerous
  half.** No rows → no series → no instance → **silence**. That covers a host that
  has never shipped (freshly built, rebuilt guest, reverted deploy) *and* a host
  dead for more than 24 h, whose series ages out and whose alert goes quiet again.
  Worse, at the 24 h boundary Grafana sends a **resolved** notification for a host
  that is still dead. `roles/vector_agent`'s deploy-time ingest assert covers the
  never-shipped case **at deploy time only** — it proves the host shipped once, not
  that it still is. Closing this needs a rule driven by an *expected-hosts list*
  rather than by observed series, which gives up the "no rule edit per host"
  property above; it is a tracked follow-up, and widening the window only moves the
  boundary.

**Absence is owned by exactly one RULE — which is not the same as one email.**
`obs-http-probe-absent` owns it: its query returns a row for every probe seen in the
last 24 hours, so an empty result means not one probe has reported in a day —
telegraf or its write path is dead. The two threshold rules therefore use
`noDataState: OK`; if they alerted on NoData too, one dead telegraf would fire three
rules instead of one.
Per *incident*, though, a dead telegraf sends **one email per probed target** —
four today — then four resolved notices, because `server` is in the root policy's
`group_by`. That is deliberate: two different services failing must be two
notifications, not one merged digest whose subject names only the first. The cost is
paid when the common cause is telegraf itself.

**Adding a probe needs no rule edit.** The probe rules carry no `server` matcher —
they aggregate `by (server, check_type)`. Add a URL to `[[inputs.http_response]]` in
`files/data/telegraf/telegraf.conf` and it becomes its own alert instance on the
next collection. Never write a `server=` matcher into those queries; that property
is the point (#139).

Detection latency: ~5 min for a failing probe (1 m interval x `for: 5m`), ~15 min
for one that goes silent (600 s of `lag` plus `for: 5m`).

The `[24h]` lookbehind on the absence rule is load-bearing and was `[1h]` until
review caught it: a series with no sample inside the lookbehind is not returned at
all, so with `[1h]` a dead probe was alertable only between 10 and 60 minutes of
silence — past an hour its instance vanished, Grafana marked it stale and
**resolved** the alert. Partial absence (one target dead, the rest healthy) is the
likely case and had no coverage at all past an hour; `noDataState: Alerting` only
catches *total* absence. Proven against the retired `http://searxng:8080` probe from
#94, which `lag[1h]` cannot see and `lag[24h]` can.
The deliberate cost: **removing a URL from `telegraf.conf` alerts for up to 24 h
afterwards** (two emails at `repeat_interval: 12h`). `[7d]` was rejected — a week of
firing after an intentional removal is alert fatigue, and a probe dead for more than
a day has already paged and been ignored.

### Notification channel

The root notification policy (`provisioning/alerting/notification-policies.yaml`)
routes **everything in the org** — not just this folder — to the `homelab-email`
contact point, which is templated by `templates/grafana-contact-points.yaml.j2`
because its address is a vault value. A `policies:` entry with no `routes:` IS the
root route, so every rule in every folder inherits it, including #108's. That is the
intent; adding a folder matcher to "scope" it would orphan every rule outside this
folder back to Grafana's stub receiver.

That contact point and Grafana's `GF_SMTP_*` settings reuse the host's **shared
Gmail relay** — the `vault_watchtower_email_*` vars, the same relay watchtower
sends release notifications through. Deliberate: it is the one SMTP path this CT
has and it is already proven to deliver from here; a second copy of the same Gmail
app password would rot independently. The coupling is enforced, not just
documented — all six var names are in the credential assert at the top of
`tasks/main.yml`, so renaming one fails the play by name instead of producing a
silent no-send.

Two mechanical consequences, both of which have bitten this repo's shape before:

- `Deploy Grafana provisioning` runs `synchronize` with `delete: true` over that
  directory, so it carries `--exclude=alerting/contact-points.yaml` **and, since
  #152, `--exclude=alerting/notification-policies.yaml`**. Remove either exclude
  and the rsync deletes the templated file every run, the template writes it back,
  and Grafana restarts on every deploy forever.
- A provisioned root policy and provisioned contact points are **read-only in the
  Grafana UI**. Routing changes go in these files.

#### The second channel, and why one channel is not a control (#152)

Until #152 there was exactly one channel and **nothing watched it**. If the Gmail
app password were revoked or rotated, if Google blocked the sender, or if egress to
587 were filtered, every alert in the stack would fail silently into
`docker logs grafana` — including `obs-log-ingest-stalled` and
`obs-host-log-ingest-stalled`, whose entire job is reporting that the log pipeline
is dead. The primary signal and its watchdog go quiet at the same moment.

The obvious fix cannot stand alone: **a rule that emails you to say email is broken
is not a control.** So the load-bearing half is a second, independent channel.

`observability_alert_telegram` (role default `false`, **`true` on eq12_docker**)
gates everything Telegram-shaped. False, delivery is email-only — the same
*channel coverage* #139 shipped, though not the same routing tree, since the
flag-false render still carries the heartbeat, thermal and severity routes (#152,
#176). So this role
still deploys on a host whose vault has no Telegram keys. True, it
requires `vault_grafana_telegram_bot_token` and `vault_grafana_telegram_chat_id`,
both asserted BY NAME at the top of the play — a blank key fails the deploy loudly
rather than provisioning a contact point that can never send. (The chat id is
stored as a **quoted string** on purpose: unquoted it parses as an integer and
Grafana's contact point wants the textual form.)

Three receivers and a five-route tree, order-sensitive (first match wins, no
`continue` anywhere):

| route matcher | receiver | channels | why |
| ------------- | -------- | -------- | --- |
| `channel = telegram-only` **and** `integration = telegram` | `homelab-email` | email | a **Telegram** delivery failure must not be reported by Telegram. More specific, so it goes FIRST |
| `channel = telegram-only` | `homelab-telegram` | Telegram | everything else carrying that label (in practice `integration = email`). Second of the pair, and must stay second — promoted, it would swallow the Telegram case |
| `heartbeat = true` | `homelab-critical` | email + Telegram, `repeat_interval: 24h` | the dead-man's switch, below |
| `component = nas-thermal` **and** `severity = warning` | `homelab-critical` | email + Telegram | thermal warnings page immediately despite warning severity (#176) — the matcher is the **component**, so no other warning is promoted. It cannot collide with the row below: that one needs `severity = critical` |
| `severity = critical` | `homelab-critical` | email + Telegram | critical rules page on both |
| *(root, no matcher)* | `homelab-email` | email | everything else, unchanged from #139 |

The `channel = telegram-only` label reads as **"route me off the failing
channel"**, not "always Telegram" — the name is historical, from before the
mirrored pair existed. The principle is symmetric: an alert reporting that a
channel failed must not travel by that channel, in *either* direction. Without the
first row, a "Telegram is broken" page would have gone out over Telegram —
reintroducing, for the Telegram half, exactly the defect #152 removed for the
email half.

`homelab-critical` **degrades to email-only** when the flag is false, which is why
the heartbeat, thermal and severity routes render unconditionally: a route pointing at a
receiver that does not exist is a **fatal** Grafana provisioning error, and this
shape means they can never dangle as the flag flips. The two `telegram-only` rows
are not rendered at all in that state, so `obs-alert-delivery-failing` is caught by
the `severity = critical` route and arrives by email — including when email is what
failed. That is the irreducible single-channel gap, and it is the reason the flag is
not meant to stay false. (`homelab-email` is unconditional, so the first row is safe
to render whenever the block is rendered at all.)

**Three rules, because each covers a failure the other two cannot see.**
`obs-alert-delivery-failing` catches "Grafana tried and the send failed".
`obs-alert-delivery-telemetry-absent` catches "the telemetry died, so the first
rule is blind" — without it, a dead telegraf or a `fieldpass` that stops matching
after a Grafana metric rename leaves the failure rule permanently NoData → **OK**:
a rule that cannot fire, reporting Normal.

> **The obvious series for that rule does not work, and finding out why is the
> most useful thing in this section.** The natural choice is
> `lag(grafana_alerting_notifications_total[24h])`, on the reasoning that telegraf
> writes it every 60s whether or not it increments. Telegraf can only write what
> Grafana *exports*, and Grafana exports that counter **only transiently**, around
> the time notifications are actually sent. Measured: sampled every 30s for 4.5
> minutes on a healthy idle instance, `grafana_alerting_notifications_total` was
> absent from **every** sample, while `grafana_alerting_alertmanager_receivers`
> and `grafana_alerting_active_configurations` were present in all of them; in
> VictoriaMetrics the notifications series exists for about six minutes after a
> notification and then vanishes for the rest of a completely healthy hour. An
> absence rule on it would fire forever — the third instance of the category error
> this repo has already shipped twice.
>
> A trap inside the trap: an instant query issued *within five minutes* of the
> last sample still returns it, because that is VictoriaMetrics' default lookback,
> and the result is stamped with the *query* time rather than the sample's. A
> vanished series looks alive for five more minutes — long enough to "confirm" it
> during a deploy and be wrong. The general rule and its measurements live in
> [docs/solutions/conventions/instant-query-cannot-prove-a-series-is-live.md](../../../../docs/solutions/conventions/instant-query-cannot-prove-a-series-is-live.md);
> choosing a series that can carry an absence rule at all is
> [absence-alerts-need-a-continuously-exported-sentinel.md](../../../../docs/solutions/conventions/absence-alerts-need-a-continuously-exported-sentinel.md).

So the rule watches `grafana_alerting_alertmanager_receivers`, which Grafana
exports for its whole process lifetime and which travels the identical path
(grafana `/metrics` → telegraf → VictoriaMetrics). Its *value* is a bonus signal:
`state="active"` should equal the number of provisioned receivers. Its
`noDataState: Alerting` is genuinely false-alarm-free on that series, which it
would not have been on the counter — and it must stay inside telegraf's
`fieldpass`, which is a hand-coupled pair called out in both files.

The heartbeat still matters to this rule, but not as a dependency: it is what
guarantees the *notifications* counter appears at least once a day, which is what
makes `obs-alert-delivery-failing` able to observe a failure at all.

**`obs-alert-delivery-heartbeat` is supposed to fire forever.** It is not broken. It
is a dead-man's switch: one notification per channel per 24 hours, so a channel
going quiet for more than a day is that channel's failure signal — and noticing it
costs nothing from the broken channel. A counter cannot cover this case (Grafana can
believe it delivered), which is why the heartbeat exists alongside
`obs-alert-delivery-failing` rather than instead of it. Same principle as #134's
host-log heartbeat, one layer up: absence of incidental traffic proves nothing, so
make the signal deliberate. Do not "fix" it by making it resolve, and do not silence
it — silencing removes the only control that does not depend on the channel it tests.

#### Drilling the delivery chain (done once, 2026-08-22 — repeat after changing routing)

Config that validates is not delivery that works, so the whole chain was exercised
once against a real failure: **broken send → Grafana counter → telegraf scrape →
VictoriaMetrics → rule fires → route → message on the *other* channel.**

The recipe, and the two dead ends that are worth knowing before you repeat it:

1. Deploy with `-e vault_watchtower_email_server=smtp.invalid`, scoped
   `--tags observability` so only Grafana's `.env` moves (watchtower's own config
   is not in that play).
2. **A restart alone does not produce a notification.** Grafana persists alert
   state across restarts, and `repeat_interval` (24h on the heartbeat route)
   suppresses a re-send for an instance that has already notified. Eight minutes of
   polling after the broken deploy showed nothing at all.
3. **Grafana's per-receiver test endpoint does not move the counters.** It reports
   the send accurately — `status: failure` in 50 ms against `smtp.invalid`,
   `status: success` in 1.2 s against the real relay — but it bypasses the
   notification pipeline, so `grafana_alerting_notifications_*` never changes. It
   is a good SMTP check and a useless delivery-monitoring check, which is a second
   reason not to build anything on it (the first is in
   [grafana-alerting-provisioned-but-undeliverable](../../../../docs/solutions/integration-issues/grafana-alerting-provisioned-but-undeliverable.md)).
   Note the endpoint name is the **unpadded** base64 of the receiver title —
   `homelab-email` → `aG9tZWxhYi1lbWFpbA`, no `==`, or it 404s.
4. What works: force a **new alert instance**, by adding a temporary label to the
   heartbeat rule (its labels are its identity, so a new label is a new instance
   with no notification history). Apply, then watch.

Measured result, with email broken and Telegram live throughout:

```
06:32  grafana  notifications_failed_total{email}=1  notifications_total{telegram}=1
06:34  VM       failed_total{email}=1                      (scrape + remote write)
06:37  rule     obs-alert-delivery-failing = firing        (VM -> rule)
06:37  grafana  notifications_total{telegram}=1 -> 3       (routed to the OTHER channel)
```

Restore by re-deploying without the override; prove it, do not assume it — a test
send returned `success` in 1.2 s (a real Gmail round trip) and the next heartbeat
notification incremented `{email}` again with no new `failed_total`. The
delivery-failing rule keeps firing for up to 15 minutes afterwards, because that is
its `increase()` window, and then resolves itself.

Telegraf scrapes Grafana's `/metrics` for the delivery counters (nothing did
before). Two measured facts live in `telegraf.conf` because both are traps:
`metric_version` 1 and 2 produce the *reverse* of the obvious shapes, and the
`*pass` filters match the **measurement** name — which under `metric_version = 2`
is the literal string `prometheus` for every metric, so `namepass` matches nothing
and the input silently yields **zero** metrics. It must be `fieldpass`. Filtering
itself is not optional: Grafana exposes 505 metric names / 3615 series into a
5-year-retention TSDB.

Datasource **uids are pinned** in `provisioning/datasources/datasources.yaml`
because alert rules reference datasources by uid, never by name. Those strings are
identities: changing one re-points every dashboard and alert that uses it.

> `http://grafana:3000` reports **301**, and 301 is inside the accepted range on
> purpose: `grafana.ini` sets `enforce_domain = true`, so every request that does
> not use the configured domain is redirected. Do not "fix" the bad-status rule by
> narrowing it to 2xx. The same setting means every Grafana **API** call by IP must
> send `-H 'Host: grafana.moutovkin.com'` (`/api/health` is exempt, which is why the
> role's readiness gate works without it).

## Firewall (:8089 and :9428)

### Port manifest — what is governed, what is open, and why

Every port this stack publishes, with its posture stated explicitly. The point of
the table is that "not in the allowlist" is a **decision with a reason**, never an
oversight — an ungoverned port here has been looked at and left open on purpose.

| Port | Posture | Auth | Why |
| ---- | ------- | ---- | --- |
| **8089** VM InfluxDB | **governed** (tcp+udp) | **none** — `--httpAuth` guards `:8428` only | An unauthenticated *write* endpoint. Nothing but the allowlist stands between the LAN and it (#122). |
| **9428** VictoriaLogs | **governed** (tcp+udp) | basic, cleartext | Became a fleet ingest endpoint in #134: machine-to-machine writes, on a schedule, carrying cleartext credentials. The allowlist is the compensating control until TLS lands. |
| **8428** VictoriaMetrics | open | basic, cleartext | Authenticated read surface. Same credential-over-cleartext class as `:9428` **minus** the scheduled machine traffic. Its client set has not been established the way `:9428`'s was; narrowing it needs the same NPM-table check and its own blocked-source proof. Tracked separately. |
| **3000** Grafana | open | admin user/password | Authenticated UI, reached by humans and by NPM. Same reasoning as `:8428`. |

The asymmetry between `:9428` and `:8428` is deliberate and is the whole content
of this table: they carry the same class of credential, and only one of them
changed what kind of traffic it receives. Do not extend the `:9428` allowlist to
the other two by analogy — establish their clients first.


`--influxListenAddr=:8089` is an **unauthenticated write endpoint** on TCP *and*
UDP — `--httpAuth.username/password` guards `:8428` only. Its one intended client is
the Home Assistant VM, so the role installs a scoped nftables table
(`inet observability_fw`) that drops :8089 from every source outside
`observability_firewall.ports[8089]` in host_vars (#122).

The table is built by the **shared `roles/nft_scoped_fw`** role (#114), included with
`nft_fw_name: observability` — which yields exactly these identifiers: table
`inet observability_fw`, file `/etc/nftables.d/observability-firewall.nft`, unit
`observability-firewall.service`. This consumer is the reason that role takes
`nft_fw_protocols`: :8089 is governed over **`[tcp, udp]`**, and the terminal
`<proto> dport != {…} accept` rules are emitted one per protocol, all ahead of the
scope guard, so a packet of neither protocol still falls through to `policy accept`.

Shape (identical to the postgres/vaultwarden precedent, see
[docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md](../../../../docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md)):
`hook prerouting` at priority **-150** (before Docker's dstnat at -100 — an
`input` hook never sees DNAT'd published ports), policy `accept`, only the governed
ports ever dropped, and unloading the table leaves those ports open rather than the
host unreachable.

### :9428 is governed too, since #134

The **LAN is one flat `192.168.0.0/18`** with no segmentation between hypervisors,
guests and workstations. Nothing else constrains who can reach a published port, so
this nftables table is the only control there is — which is why #134 narrowed
`:9428` in the same change that turned it from a port only this host's own container
wrote to into a **fleet ingest endpoint** accepting writes from three other hosts.

The allowlist is built from **configuration facts, not observed traffic**, and
deliberately: measured 2026-08-20, the `:9428` DNAT rule had passed 3 packets in the
~24 h since the container started, and conntrack held no `:9428` flows at all. A
sample that size would have "proved" nothing needs access.

| Source | Why | Evidence |
| ------ | --- | -------- |
| `192.168.25.5/32` | eq12 agent | writer, `roles/vector_agent` |
| `192.168.30.5/32` | n5pro agent | writer, `roles/vector_agent` |
| `192.168.30.15/32` | n5pro_docker agent | writer, `roles/vector_agent` |
| `192.168.25.20/32` | NPM | **verified**: CT 104's `proxy_host` row id 11, `vl.moutovkin.com` → `deb-docker.lan:9428`, `is_deleted=0` |
| `192.168.48.0/24` | operators | VictoriaLogs UI at `/select/vmui` |

NPM was checked, not assumed — #140 established that inherited NPM grants in this
repo have been wrong before, so this went to CT 104's own `proxy_host` table the
same way. Unlike #140's portainer case, the answer came back **yes**. Deleting that
proxy host is what would make the grant stale.

**Grafana is unaffected and gets no grant**, confirmed rather than asserted: its
provisioned datasource URL is `http://victorialogs:9428`, the compose service name
on `172.20.0.0/24`, so its queries never traverse `eth0`. The same is true of the
local `vector` and `telegraf` containers, and the role's own ingest assert uses
`localhost` (covered by the table's `iif "lo"` rule).

`:8428` (VictoriaMetrics) and `:3000` (Grafana) stay **ungoverned**. That is now a
live asymmetry rather than a blanket "read ports are open" rule — this section used
to lump `:9428` in with them. Narrowing those needs the same NPM-table check and its
own from-a-blocked-source verification; it is tracked separately, and the `:9428`
list must not be extended to them by analogy.

Note the ingest path is **plain HTTP with basic auth**, like every other internal
hop in this homelab, so these credentials cross the LAN in cleartext. VictoriaLogs'
`--httpAuth` is one credential pair for the whole instance, so every shipping host
necessarily holds read+write credentials — which is why they live in
`group_vars/all/vault.yml`. TLS on the ingest path is a tracked follow-up, stated
here rather than shipped quietly.

```bash
ssh root@192.168.25.15 'nft list table inet observability_fw'   # read-only check
```

Verify from a **blocked** source, not an allowed one — an allowed source proves
nothing about the drop rule. Two traps here, both real:

- **This repo's operator workstations are inside `192.168.48.0/24`, which is on the
  allowlist.** A `curl` from the machine running Ansible is an *allowed* source and
  proves nothing. The blocked source used for `:9428` was the **Home Assistant VM**
  (`192.168.25.10`), reached over the hypervisor console — it is granted `:8089`
  only, so it exercises the per-port shape at the same time.
- **The drop rules carry a `counter` (`nft_fw_count_drops`), and a counter is not
  the proof.** It shows a packet *matched* the rule, not that a blocked source
  exists to generate one; a counter sitting at 0 reads exactly like a rule nothing
  ever tried to traverse. Use it to confirm the rule keeps firing after later
  reloads, never as the verification itself.

## Metric attribution — the `host` label (#178)

Every metric in VictoriaMetrics answers "which machine is this from?" the same
way: **`host` = Ansible `inventory_hostname`**, plus `host="truenas"` for the
appliance. Three producers, one convention:

| producer | how `host` is set | value here |
| -------- | ----------------- | ---------- |
| telegraf (`[agent] hostname`) | `${TELEGRAF_HOSTNAME}` from `.env` (`inventory_hostname`), passed through by `compose.yaml` | `eq12_docker` |
| Vector (containerized + native agents) | `${VECTOR_HOSTNAME}` from `.env`, same expression | `eq12_docker`, `eq12`, `n5pro`, `n5pro_docker` |
| TrueNAS (graphite stream + API poller) | exporter `namespace` segment → `host` label; poller sets it itself | `truenas` |

Before #178 telegraf pinned the literal `hostname = "homelab-telegraf"`, a name
matching no machine, so eq12_docker's own vitals were unattributable.
**Label boundary 2026-08-24T03:14Z** (= 2026-08-23 20:14 PDT — stated in UTC
because that is what VictoriaMetrics displays, and the local date is a day
earlier): samples before that instant carry `host="homelab-telegraf"`. Nothing consumed the old value (audited), so the break
was accepted rather than shimmed.

Two traps live in `telegraf.conf` because of it, both measured on the running
1.39.3 image rather than assumed:

- **An unset `TELEGRAF_HOSTNAME` is not an error.** `telegraf --test` with the
  variable absent from the container env emits `host=${TELEGRAF_HOSTNAME}` — the
  literal string — with exit 0 and a healthy agent; with it empty it falls back to
  `os.Hostname()`, a 12-char container id. 1.38+ "strict environment variable
  handling" catches neither. The `:?` guard in `compose.yaml` covers the
  `.env` → compose hop; `obs-telegraf-metrics-absent` covers the rest.
- **`taginclude` is an allowlist over the FINAL tag set.** `[[inputs.docker]]`
  had `taginclude = ["container_id", "container_name", "container_image"]`, which
  is applied *after* the agent adds `host` and `[global_tags]` — so all 448
  `docker_*` series arrived with no host, no environment and no location, for the
  life of the deployment, with no error anywhere. `"host"` is in that list now and
  must stay: `container_name` is not unique across machines, so without it two
  hosts' per-container series merge silently.

## Log record schema

What Vector stores in VictoriaLogs, per record (#143). Only the **labels** become
part of `_stream`; everything else is a plain JSON field — which VictoriaLogs still
makes queryable, so `level:err` works without paying for stream cardinality.

| Field | Label? | Sources | Example | Origin |
| ----- | ------ | ------- | ------- | ------ |
| `source` | **label** | all | `host` / `docker` / `pkg` | literal, set per transform |
| `hostname` | **label** | all | `eq12_docker` | `VECTOR_HOSTNAME` from `.env` (Ansible `inventory_hostname`) |
| `host` | no | all | `eq12_docker` | same value; VictoriaLogs stores both keys and both used to be wrong |
| `container_name` | **label** (docker only) | docker | `grafana` | Docker daemon metadata |
| `level` | no | all | `err`, `warning`, `notice`, `info` | RFC5424 `<PRI>`; literal `info` for docker/pkg |
| `facility` | no | host | `authpriv`, `daemon` | RFC5424 `<PRI>` |
| `appname` | no | host | `sshd` | RFC5424 header |
| `procid` | no | host | `1234` (`null` when the header says `-`) | RFC5424 header |
| `syslog_hostname` | no | host | `deb-docker` | RFC5424 header — the CT's real OS hostname |
| `parse_failed` | no | host | `syslog` | set only when a line is not RFC5424; `level` is then `unknown`, never a fabricated `info` |
| `timestamp_source` | no | all | `event` / `ingest` | provenance of `_time`. `ingest` = the record carried no time of its own. **Was `event` for every record until #154** — see below |
| `file`, `source_type` | no | host, pkg | `/var/log/structured.log`, `file` | Vector's file source |
| `_msg` | — | all | the message text | `message`, with the RFC5424 header stripped on host records |
| `_time` | — | all | — | host: the line's own timestamp, to the microsecond. docker: the **Docker daemon's** log timestamp. pkg: the line's own timestamp where the line carries one, ingestion time otherwise — check `timestamp_source` (see limitation 4) |

> **`timestamp_source` used to be unable to say `ingest`, and nothing reported
> it.** `finalize` stamps `now()` + `ingest` only when nothing upstream supplied a
> timestamp — but BOTH upstream sources pre-set one: `docker_logs` from the
> daemon's log timestamp (legitimate, that IS the event time) and the `file`
> source from the **read** time (not an event time at all). So the
> `!is_timestamp` branch never fired for host or pkg records and every single one
> was labelled `event`. Measured 2026-08-22 UTC — dates in this README are UTC,
> which is why late-evening local (America/Los_Angeles) measurements carry the
> next day's date; the pkg examples below deliberately show both. 1672 pkg records claiming `event`
> while carrying ingest time, 0 claiming `ingest`, ever. The one field built to
> make that distinction queryable was the one field that could not express it —
> and because the value it reported was a plausible one, nothing looked wrong.
> #154 adds `del(.timestamp)` to `parse_pkg`'s and `parse_host`'s no-timestamp
> branches, which is what makes the fallback reachable and the label honest.

`hostname` is deliberately the **Ansible inventory name**, not the container ID
(`get_hostname!()`, the old value — it changed on every recreate) and not the CT's
OS hostname. Per-host alerting (#134) keys on it, and an alert must be written
against the name the inventory uses or it needs a second, hand-maintained mapping.

### How host records get a severity

Debian writes `/var/log/syslog` and `/var/log/auth.log` in `RSYSLOG_FileFormat`,
which carries **no `<PRI>`** — severity and facility are simply not in those lines
and cannot be parsed out. So the role deploys
`/etc/rsyslog.d/40-vector-structured.conf` (from `roles/rsyslog_structured/files/rsyslog-vector-structured.conf`),
which re-emits every message in **RFC5424** to `/var/log/structured.log`, and Vector
tails *that* instead. `parse_syslog()` then yields severity, facility, appname,
procid, hostname and the real timestamp.

`*.*` in that drop-in is exactly Debian's `*.*;auth,authpriv.none` (syslog) plus
`auth,authpriv.*` (auth.log), so the one file replaces both — and `vector.yaml` must
**not** also tail syslog/auth.log or every host event is ingested twice. The
human-facing files are untouched in format and content; growth of the duplicate is
bounded by `/etc/logrotate.d/vector-structured` (weekly, `rotate 4`).

Backfill posture: both file sources use `read_from: beginning` with **no**
`ignore_older_secs`. The old `end` + 600 s pair made any Vector outage longer than
ten minutes a permanent hole even though the files on disk were intact — including
after a [buffer reset](#recovering-a-wedged-vector-buffer), which is supposed to be
the recovery. The cost is the mirror image: a reset wipes checkpoints, so
`structured.log` is re-read from its start and up to one logrotate period is
duplicated.

### Known limitations (four; two now closed or bounded)

1. **No kernel or OOM-kill events — on eq12_docker. PARTIALLY CLOSED fleet-wide by
   #134.** This entry used to say "there is no kernel ring buffer in an LXC", and
   that generalisation is wrong; the corrected, measured claim (2026-08-19) is:

   | Host | `/var/log/kern.log` | Kernel events reach VictoriaLogs |
   | ---- | ------------------- | -------------------------------- |
   | `eq12_docker` (CT 101, **unprivileged**) | does not exist | **no** — `imklog` produces nothing here |
   | `n5pro_docker` (CT 201, **privileged**) | exists, carries live `veth*`/`docker0` lines | yes, since #134 |
   | `eq12`, `n5pro` (hypervisors) | exist | yes, since #134 |

   So it is privilege, not LXC-ness, that decides it. On the three hosts that do
   get kernel messages, they arrive **through the `*.*` selector in the structured
   drop-in**, which carries the `kern` facility — *not* through a `kern.log`
   source. `roles/vector_agent`'s config deliberately does not tail `kern.log`
   either: that would double-ingest every kernel line the selector already carries.
   The gap that remains is eq12_docker's, and it is structural.
2. **Boot-window blind spot — NOT closed.** #143 asked for a `journald` source to
   fix two things at once: the lost structured metadata, and units that log before
   `rsyslog.service` starts. Only the first half is solved (RFC5424 gives us
   `level`, `facility`, `appname`, `procid`, `syslog_hostname`). The second half
   stands: pre-rsyslog units are in the journal and reach neither file. A journald
   source cannot close it here, because Vector's journald source shells out to the
   `journalctl` binary and the image we run
   (`timberio/vector:latest-distroless-static`) has no shell and no `journalctl` —
   proven, not assumed:
   `docker exec vector journalctl --version` → `executable file not found in $PATH`
   (rc=127). `latest-debian` does not ship it either, so closing this gap means
   building a custom image. Measured 2026-08-18, journal and syslog+auth agreed
   exactly (968 lines each), so today's practical loss is a handful of early-boot
   lines — but it is a real gap, not a solved one.
3. **Vector ingests its own stderr. BOUNDED since #153, not removed — and it did
   not start with #143.** The README used to say #143 introduced this behaviour,
   because the measurement available at the time (zero `container_name:vector`
   records in the preceding 7 days) looked like proof it had never happened. #153's
   follow-up measured the whole retention window instead, and that reading was
   wrong:

   | window | records | hostname label |
   | ------ | ------- | -------------- |
   | 2026-06-04 → 2026-06-20T21:05 | **16,864** (~1000/day) | `751be39a3615` — the vector container's own id, what `get_hostname!()` wrote before #143 relabelled every record |
   | 2026-06-20T21:05 → 2026-08-20T03:39 | **0** | — |
   | since 2026-08-20T03:39 (the #143 deploy) | **153** across the 7 days to 2026-08-21 (~26/day) | `eq12_docker` |

   So self-ingestion had merely been **off for two months**, and the 7-day query
   measured a real but old absence. It is not a version boundary either: Vector
   0.56.0 was running on both sides of the 2026-06-20T21:04 restart, and the 0.57
   upgrade came 25 days later. Today's ~26/day is ~38x **below** the ~1000/day
   this same store absorbed for 16 days without incident. **What toggled it off and back
   on is undetermined, and no guess is recorded here.**

   `docker_logs` does not self-exclude, and `exclude_containers: ["vector"]` is
   deliberately NOT applied: it would delete Vector's own diagnostics from
   VictoriaLogs — the first place anyone looks when this pipeline misbehaves, and
   the only copy that survives a container recreate (`docker logs` does not). Both
   prior deaths of this pipeline were diagnosed from exactly those lines.

   What #153 does instead is bound the **amplification**, which is the actual
   hazard: a `throttle` transform between `docker_logs` and `parse_docker` caps the
   vector-container branch at **30 events/60s** (`exclude: '.container_name !=
   "vector"'`, so every other container bypasses it untouched). Put in
   proportion: current volume is ~26 records per **day**, so one 60-second
   allowance already exceeds a full day's traffic and the sustained ceiling is
   43,200/day — roughly 1,600x what this branch produces. It cannot engage in
   health, and stays generous enough that a crash loop's startup burst
   (~15-25 lines/attempt) remains greppable rather than being clipped exactly when
   it is most wanted. That last case is also a documented false lead: a crash loop
   trips the throttle with no flood at all, so `obs-vector-discarding-events` says
   to disambiguate with the ingest-stalled and metrics-absent rules. Every event it drops increments
   `component_discarded_events_total{component_id="throttle_vector_own"}`, which
   `obs-vector-discarding-events` (#151) pages on — **the throttle engaging IS the
   flood alarm**, not a silent loss. #151 and #153 compose: one bounds the loop, the
   other announces it.
4. **Package-audit records carry the line's own time where the line has one
   (#154). CLOSED, with a retention caveat.** `dpkg.log`, `apt/history.log` and the
   two `unattended-upgrades` logs are not syslog-shaped, so until #154 nothing
   extracted their timestamps and `_time` was when Vector *read* the line — on the
   #143 backfill, all 293 records inside a 3.8 ms window while their `_msg` values
   carried dates days older. Worse, it compounded on every buffer reset:
   checkpoints wiped, files re-read from the start, and Debian's monthly
   `rotate 12` means a reset could re-ingest up to a **year** of package history and
   drag its apparent dates forward each time.

   `parse_pkg` now parses four measured formats — `dpkg.log`'s and
   `unattended-upgrades.log`'s leading `YYYY-MM-DD HH:MM:SS`, and the
   `Start-Date:` / `End-Date:` / `Log started:` / `Log ended:` markers, which use
   **two** spaces between date and time. Lines with no timestamp at all (apt
   history block bodies, dpkg progress spam) correctly keep ingest time and are
   now genuinely queryable as `timestamp_source:"ingest"` — see the note under the
   schema table for why that label was previously impossible to produce.

   **The timezone pin is load-bearing, and it is the part that would have shipped
   silently wrong.** These formats carry no zone marker, so they resolve through
   Vector's global `timezone`, which defaults to `local` — and the container image
   is `distroless-static` with no OS tzdata, so `local` resolves to **UTC**.
   Measured against the binary:

   ```
   vector vrl -z local               'parse_timestamp!("2026-08-16 06:19:24", …)'  -> 2026-08-16T06:19:24Z   (7h early)
   vector vrl -z America/Los_Angeles  (same input)                                 -> 2026-08-16T13:19:24Z   (correct)
   ```

   Both `vector.yaml` and `roles/vector_agent`'s template therefore pin `timezone`
   explicitly (the agent from `group_vars/all`'s `timezone`); Vector bundles
   chrono-tz, so a named zone needs no tzdata on disk. Only zone-less
   `parse_timestamp` calls read it — `parse_syslog`'s RFC5424 timestamps carry
   their own offset and are untouched.

   **Caveat, real and not worth hiding:** a backfilled line older than
   VictoriaLogs' 90-day retention is dropped on ingest. The package audit trail's
   depth is therefore the *retention* depth, not the log files' depth, even though
   `dpkg.log` on disk goes back further. What is fixed is that history no longer
   gets dragged forward by a buffer reset.

## Fleet coverage (#134)

This section used to say the collector ran on eq12_docker only and that the other
three hosts shipped nothing. **That gap is closed.** All four hosts now ship to
this VictoriaLogs, with the same record schema:

| `hostname` label | Host | Shipper | Deployed by |
| ---------------- | ---- | ------- | ----------- |
| `eq12_docker` | CT 101, 192.168.25.15 | the `vector` **container**, in this compose stack | this role |
| `eq12` | Proxmox hypervisor, 192.168.25.5 | native systemd `vector.service` | [`roles/vector_agent`](../../vector_agent/README.md) |
| `n5pro` | Proxmox hypervisor, 192.168.30.5 | native systemd `vector.service` | `roles/vector_agent` |
| `n5pro_docker` | CT 201, 192.168.30.15 | native systemd `vector.service` (+ its docker logs) | `roles/vector_agent` |

**`pve` is not a hostname label.** #134's issue text calls that host "pve", which is
its Proxmox node name and its OS hostname; the *inventory* name is `eq12`, and
`VECTOR_HOSTNAME` is `inventory_hostname`. `pve` appears in records only as the
`syslog_hostname` field. A query or alert written against `hostname:pve` matches
nothing, forever, silently.

On every host, `vector.yaml` tails `/var/log/structured.log` with **no filtering**
(so every syslog-writing daemon reaches VictoriaLogs automatically) plus the four
package-audit files listed above. The three agents deploy that stream from the same
shared [`roles/rsyslog_structured`](../../rsyslog_structured/README.md) this role
uses.

The agents' config is a near-duplicate of `files/data/vector/vector.yaml`, living at
`roles/vector_agent/templates/vector.yaml.j2`. **Change one, change both**, and keep
the schema table above in step with them. Unifying the two is a follow-up issue.
#154's `parse_pkg` timestamp parsing and its `timezone` pin landed in both for
exactly this reason — they change record `_time` semantics, so the two configs
could not be allowed to drift apart on it.

One thing that is deliberately **not** symmetric: #151's `internal_metrics` source
and its VictoriaMetrics sink exist only in the container's config. The four
`obs-vector-*` rules therefore cover eq12_docker's Vector and no other. Extending
self-telemetry to the three agents is a follow-up, not an oversight; their coverage
today is `obs-host-log-ingest-stalled-per-host` plus `roles/vector_agent`'s own
end-to-end ingest assert on every run.

`:9428` gained three remote writers and no new exposure — see
[PORT_REFERENCE.md](PORT_REFERENCE.md#port-9428-has-remote-writers-since-134) for
the writer table, why the port was not narrowed, and the cleartext-basic-auth
posture. The `vault_vl_auth_*` credentials moved from `host_vars/eq12_docker` to
`group_vars/all/vault.yml` so the three agents can see them (moved, not copied — a
second copy of a secret rots independently).

## Home Assistant integration

**There isn't one.** This section used to say HA writes over the InfluxDB v1 line
protocol to `192.168.25.15:8089`. It never has — proven four independent ways by
the #133 diagnosis (2026-08-20):

- `vm_rows_inserted_total{type="influx"} = 0` since process start, against
  `{type="promremotewrite"} = 734277` (telegraf's path);
- no HA-shaped series has ever existed over the full 5 y store — `entity_id`,
  `domain`, `friendly_name` label values are all `[]`, and the complete
  `__name__` set is 162 telegraf metrics;
- the DOCKER-chain DNAT counters on both `:8089` rules read `packets 0`, while
  the `:8428` rule on the same chain shows traffic (so the counters work);
- a 90 s tcpdump on CT 101's eth0 saw zero packets, and HA's own 258 MB archived
  log contains zero occurrences of "influx".

`configuration.yaml` on VM 100 has no `influxdb:` block and never has, back to the
earliest snapshot in version control. So the `:8089` listener, its TCP+UDP port
publishes, the 5-year retention "for HA historical data",
`--dedup.minScrapeInterval=15s` "smooths HA transients", and #122's
`observability_firewall` allowlist for `192.168.25.10/32` are all real
infrastructure built for a writer that does not exist. **#122's allowlist is not
implicated** — `192.168.25.10` is explicitly accepted for both protocols and its
drop rules have never been reached; HA can reach the box fine
(`curl http://192.168.25.15:8428/health` from inside VM 100 → 200 in 0.7 ms).

If the export is ever wanted, it must target **8428** — the authenticated
InfluxDB v1 HTTP API — not 8089, which is a raw line-protocol socket that cannot
answer HA's HTTP client. Config snippet and the verified endpoint behaviour:
[PORT_REFERENCE.md](PORT_REFERENCE.md#the-working-path-for-an-influxdb-http-client-port-8428).

**#133 stays open** and nothing about the running configuration was changed by
this correction: it removes a recipe the repo documented and we proved cannot
work. The two decisions the human still owns are whether to keep
`--dedup.minScrapeInterval=15s` (it silently collapses HA state changes < 15 s
apart, which is lossy for event-driven entities) and whether to drop the `:8089`
listener entirely, which would delete an unauthenticated write endpoint and make
#122 moot.

## Troubleshooting

```bash
ssh root@192.168.25.15 'docker ps --format "table {{.Names}}\t{{.Status}}"'
ssh root@192.168.25.15 'docker logs --tail 50 vector'
ssh root@192.168.25.15 'docker exec telegraf ping -c1 victoriametrics'
```

- **Grafana cannot reach a datasource** — the URL must be the compose service name
  (`http://victoriametrics:8428`), not `localhost`.
- **VM/VL return 401** — the templated `.env` values and the provisioned datasource
  credentials disagree; re-deploy rather than editing either on the host.
- **`victorialogs` shows no healthcheck** — correct and deliberate: it is a
  `scratch` image with no shell or wget, so any exec probe would report it
  permanently unhealthy. Nothing may `depends_on: service_healthy` it.

## Resources

- [VictoriaMetrics docs](https://docs.victoriametrics.com/) ·
  [MetricsQL](https://docs.victoriametrics.com/MetricsQL.html)
- [VictoriaLogs docs](https://docs.victoriametrics.com/VictoriaLogs/) ·
  [LogsQL](https://docs.victoriametrics.com/VictoriaLogs/LogsQL.html)
- [Vector](https://vector.dev/docs/) · [Telegraf](https://docs.influxdata.com/telegraf/v1/) ·
  [Grafana](https://grafana.com/docs/grafana/latest/)

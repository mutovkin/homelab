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
                ├── ingest-health.yaml         "log ingest stalled" rule (#108)
                ├── probe-health.yaml          three http_response rules (#139)
                └── notification-policies.yaml root policy → homelab-email (#139)
                    (contact-points.yaml is TEMPLATED, not a file here — see
                     templates/grafana-contact-points.yaml.j2)
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

Six provisioned rules, all in the **Observability** folder, all delivered by
email. (This table listed four and omitted `obs-host-log-ingest-stalled`, which has
existed since #143; #134 added the sixth and corrected the count.)

| uid | rule | fires when | noData |
| --- | ---- | ---------- | ------ |
| `obs-log-ingest-stalled` | VictoriaLogs ingest stalled | `_time:5m \| count()` < 1, `for: 10m` | **Alerting** |
| `obs-host-log-ingest-stalled` | Host log ingest stalled (fleet-wide) | `_time:10m source:host \| count()` < 1, `for: 10m` | **Alerting** |
| `obs-host-log-ingest-stalled-per-host` | A host stopped shipping logs | `_time:24h source:host \| stats by (hostname) count() if (_time:15m)` < 1, `for: 10m` | OK |
| `obs-http-probe-failing` | HTTP probe failing | `max by (server, check_type) (http_response_result_code)` > 0, `for: 5m` | OK |
| `obs-http-probe-bad-status` | HTTP probe returning a bad status | `max by (server, check_type) (http_response_http_response_code)` outside 200-399, `for: 5m` | OK |
| `obs-http-probe-absent` | HTTP probe stopped reporting | `min by (server, check_type) (lag(http_response_result_code[24h]))` > 600, `for: 5m` | **Alerting** |

`execErrState: Alerting` on all six — a datasource that cannot be reached is not
evidence of health.

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
_time:24h source:host | stats by (hostname) count() if (_time:15m) as recent
```

A dead shipper is still in the 24 h window, so it returns a row with `recent = 0` —
which a `< 1` threshold can fire on. Measured 2026-08-19 against the live
VictoriaLogs, running the deployed expression and then the naive form as a control.
The **pair** is the proof; neither line means much on its own:

```
Q1 — the expression this rule deploys
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
  directory, so it carries `--exclude=alerting/contact-points.yaml`. Remove the
  exclude and the rsync deletes the templated file every run, the template writes it
  back, and Grafana restarts on every deploy forever.
- A provisioned root policy and provisioned contact points are **read-only in the
  Grafana UI**. Routing changes go in these files.

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
| `timestamp_source` | no | all | `event` / `ingest` | provenance of `_time`, so a host record that fell through the syslog parse is queryable |
| `file`, `source_type` | no | host, pkg | `/var/log/structured.log`, `file` | Vector's file source |
| `_msg` | — | all | the message text | `message`, with the RFC5424 header stripped on host records |
| `_time` | — | all | — | host: the line's own timestamp, to the microsecond. docker: the **Docker daemon's** log timestamp. pkg: ingestion time (see limitation 4) |

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

### Known limitations (four, all deliberate)

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
3. **This change INTRODUCED Vector self-ingestion; it did not eliminate it.** Say it
   that way round, because the comfortable phrasing gets it backwards. #143 item 3 was
   filed about a self-amplifying loop — Vector re-ingesting its own noisy stderr.
   Measured, that loop was **not** happening before: zero records with
   `container_name:vector` across all seven prior container-ID hostname labels, and
   zero containing the old label-render warning, verified against control phrases from
   Vector's own stderr that *do* return hits. Measured after: **77 records and
   counting**, all under the new `eq12_docker` label, starting the moment the
   post-change container came up. `docker_logs` does not self-exclude.
   **Why self-ingestion began with this change was not determined**, and no guess is
   recorded here.
   Today it is harmless — the volume is trivial, almost all of it startup lines, and
   Vector's own internal flood suppression bounds repeats. But the honest conclusion
   is the uncomfortable one: the amplification hazard the issue was filed about is now
   **live rather than hypothetical**, and because we cannot explain why it started, we
   cannot promise it stays bounded.
   `exclude_containers: ["vector"]` on the `docker_logs` source is the one-line
   mitigation and is deliberately NOT applied: it would also delete Vector's own
   diagnostics from VictoriaLogs, the first place anyone looks when this pipeline
   misbehaves, and the only copy that survives a container recreate. Tracked as its own
   issue rather than left to this paragraph.
4. **Package-audit records carry ingestion time, not line time.** `dpkg.log`,
   `apt/history.log` and the two `unattended-upgrades` logs are not syslog-shaped and
   are tailed directly, so `_time` is when Vector read the line. This is a known
   property, not a defect: on the backfill run all 293 records landed inside a 3.8 ms
   window (`min(_time)` 2026-08-20T03:39:20.421Z, `max(_time)` …20.424Z) while their
   `_msg` values carry dates back to 2026-08-16. Host records are the opposite — their
   `_time` is the line's own timestamp, to the microsecond.

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

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
  InfluxDB, single binary, PromQL/MetricsQL, and a native InfluxDB line-protocol
  listener that Home Assistant writes to directly. 5-year retention.
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
├── tasks/main.yml                  firewall → data dirs → configs → deploy → restarts
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
            └── alerting/ingest-health.yaml    "log ingest stalled" rule (#108)
```

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

`provisioning/alerting/ingest-health.yaml` provisions **VictoriaLogs ingest
stalled**: `_time:5m | count()` over VictoriaLogs, alerting when fewer than 1 row
arrived in 5 minutes, `for: 10m`. `noDataState` and `execErrState` are both
`Alerting` — for a rule whose whole job is detecting absence, "no data" is the
failure, not the healthy case.

**No contact point is provisioned.** The rule fires and is visible under
_Alerting → Alert rules_, but nothing delivers it anywhere yet. Wiring notifications
is deliberately out of scope for #108.

Datasource **uids are pinned** in `provisioning/datasources/datasources.yaml`
because alert rules reference datasources by uid, never by name. Those strings are
identities: changing one re-points every dashboard and alert that uses it.

## Firewall (:8089)

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
`input` hook never sees DNAT'd published ports), policy `accept`, only :8089 ever
dropped, and unloading the table leaves the port open rather than the host
unreachable. The read ports 8428/9428/3000 are untouched: NPM (192.168.25.20) and
operator workstations must keep reaching them.

```bash
ssh root@192.168.25.15 'nft list table inet observability_fw'   # read-only check
```

Verify from a **blocked** source, not an allowed one — an allowed source proves
nothing about the drop rule.

## Coverage gap

The log collector (`vector`) runs on **eq12_docker only**. `pve`, `n5pro` and
`n5pro_docker` ship no logs to VictoriaLogs at all — their syslog and container
output are invisible in Grafana. This is a known fleet gap with its own issue; it is
not a misconfiguration of this role.

On eq12_docker, `vector.yaml`'s `host_logs` source tails `/var/log/syslog`,
`/var/log/auth.log` and `/var/log/kern.log` with **no filtering**, so
`unattended-upgrades` and every other syslog-writing daemon reaches VictoriaLogs
automatically.

## Home Assistant integration

HA writes over the InfluxDB v1 line protocol to `192.168.25.15:8089` (no database,
user or token needed). It must be listed under `observability_firewall.ports[8089]` —
it is, as `192.168.25.10/32`.

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

# Port Reference Guide

## VictoriaMetrics Observability Stack - Complete Port Mapping

### VictoriaMetrics (Time-Series Metrics Database)

| Port     | Protocol | Purpose                               | Used By                                         |
| -------- | -------- | ------------------------------------- | ----------------------------------------------- |
| **8428** | HTTP     | **Primary HTTP API and Web UI** (`/vmui`), incl. the **InfluxDB v1 HTTP API** | Grafana queries, Telegraf writes, Manual queries; the only working path for an InfluxDB HTTP client |
| **8089** | TCP/UDP  | **InfluxDB line protocol, raw socket** — enabled, **unauthenticated**, and **no client has ever written to it** (#133) | nothing, today |
| **2003** | TCP/UDP  | Graphite protocol — **not enabled in this deployment** | — |
| **4242** | TCP      | OpenTSDB protocol — **not enabled in this deployment** | — |

> `compose.yaml` passes only `--influxListenAddr=:8089`. There is no
> `--graphiteListenAddr` and no `--opentsdbListenAddr`, and neither port is
> published, so 2003 and 4242 are listed above only to record that they are *not*
> available here — enabling either means adding the flag and the port publish.

#### Port 8428 - Main HTTP API

- **Web UI**: http://localhost:8428/vmui
- **Health Check**: http://localhost:8428/health
- **Metrics Query**: http://localhost:8428/api/v1/query
- **Prometheus Remote Write**: http://localhost:8428/api/v1/write
- **Series Query**: http://localhost:8428/api/v1/series

#### Port 8089 - InfluxDB line protocol (raw socket) — **not for Home Assistant**

This section used to recommend 8089 for Home Assistant. **That recipe cannot
work**, and following it would have built a silently-dead export. Corrected from
the #133 diagnosis (2026-08-20), which measured every claim below.

- **It is a raw line-protocol socket, not HTTP.** Home Assistant's `influxdb:`
  integration is an HTTP client — it issues `POST /write?db=…`. There is nothing
  on 8089 to answer an HTTP request.
- **It is unauthenticated.** `--httpAuth.username/password` guards `:8428` only.
  That is not a feature; it is the security defect that forced the scoped
  nftables allowlist in #122, and it is published on **TCP and UDP**.
- **Nothing has ever written to it.** `vm_rows_inserted_total{type="influx"} = 0`
  over the full 5 y store; no HA-shaped series has ever existed; the DOCKER-chain
  DNAT counters on both 8089 rules read `packets 0`; a 90 s tcpdump saw nothing.
- **Performance claims removed.** There was no evidence for "better performance
  than HTTP API", and that sentence is what produced a listener, a 5 y retention
  setting and a firewall rule for a writer that has never sent a byte.

#### The working path for an InfluxDB HTTP client: **port 8428**

VictoriaMetrics serves the InfluxDB **v1 HTTP API** on 8428 — verified live
during the #133 diagnosis: `POST /write?db=…` → 204 authenticated / 401
unauthenticated, `GET /ping` → 204, `GET /query?q=SHOW DATABASES` → a well-formed
result, `GET /influx/health` → `status: pass`. Credentials are **required** (8428
is the authenticated port).

```yaml
influxdb:
  api_version: 1
  host: 192.168.25.15
  port: 8428                 # NOT 8089 — 8089 is a raw line-protocol socket;
                             # HA's integration is an HTTP client.
  ssl: false
  verify_ssl: false
  database: homeassistant    # VictoriaMetrics ignores the name; the client requires one
  username: !secret vm_auth_username   # = vault_vm_auth_username (eq12_docker/vault.yml)
  password: !secret vm_auth_password   # = vault_vm_auth_password
  max_retries: 3
  default_measurement: state
  include:                   # start narrow — an unfiltered HA export is a cardinality event
    domains: [sensor, binary_sensor]
```

**#133 is still open** and Home Assistant is deliberately unchanged by this
correction. What is fixed here is only the repo documenting a recipe we proved
cannot work. The `:8089` listener, its two port publishes, `--retentionPeriod=5y`,
`--dedup.minScrapeInterval=15s` and `observability_firewall.ports[8089]` all
remain exactly as they are, pending the human's decision on #133.

#### Ports 2003 (Graphite) and 4242 (OpenTSDB)

**Not enabled in this deployment.** VictoriaMetrics supports both protocols, but
`compose.yaml` passes neither `--graphiteListenAddr` nor `--opentsdbListenAddr`
and publishes neither port. Nothing is listening.

---

### VictoriaLogs (Log Aggregation Database)

| Port     | Protocol | Purpose                             | Used By                                   |
| -------- | -------- | ----------------------------------- | ----------------------------------------- |
| **9428** | HTTP     | **HTTP API and Web UI** (`/select/vmui`) | Grafana queries, Vector writes (local **and 3 remote**), NPM, manual queries |

#### Port 9428 has REMOTE writers since #134

Until #134 the only writer was the `vector` container on this host, reaching
VictoriaLogs over the compose network — nothing crossed the LAN to write. Three
native Vector agents now do:

| Writer | Source address | Path |
| ------ | -------------- | ---- |
| `vector` container (eq12_docker) | compose network `172.20.0.0/24` | `http://victorialogs:9428/insert` |
| `vector` agent on **eq12** | 192.168.25.5 | `http://192.168.25.15:9428/insert` |
| `vector` agent on **n5pro** | 192.168.30.5 | `http://192.168.25.15:9428/insert` |
| `vector` agent on **n5pro_docker** | 192.168.30.15 | `http://192.168.25.15:9428/insert` |

The endpoints are built in `ansible/inventory/group_vars/all/vars.yml` from
eq12_docker's inventory address, so a re-IP of this CT moves them automatically.

Two properties of that, both deliberate and both stated rather than shipped
quietly:

- **:9428 IS narrowed, in this same change, and no port was opened.** An earlier
  draft deferred this to a follow-up; that was overruled, correctly. The LAN is one
  flat `192.168.0.0/18` with no segmentation, so the `inet observability_fw`
  nftables table is the only thing constraining reach — and this change is exactly
  what turns `:9428` from a port only the local container wrote to into a fleet
  ingest endpoint. The allowlist is the three agents, NPM (**verified** against CT
  104's `proxy_host` table, not assumed) and the operator subnet; Grafana needs no
  entry because it reaches VictoriaLogs over the compose network. Full source list
  and evidence: the README's
  [Firewall section](README.md#9428-is-governed-too-since-134). `:8428` and `:3000`
  remain ungoverned and are tracked separately — do not extend this list to them by
  analogy.
- **The ingest path is cleartext HTTP with basic auth**, like every other internal
  hop in this homelab. VictoriaLogs' `--httpAuth` is one credential pair for the
  whole instance, so each shipping host necessarily holds read+write credentials
  (which is why they live in `group_vars/all/vault.yml`). TLS on the ingest path
  is a follow-up issue.

#### Port 9428 - Main HTTP API

- **Web UI**: http://localhost:9428/select/vmui
- **Health Check**: http://localhost:9428/health
- **Log Query**: http://localhost:9428/select/logsql/query
- **Log Ingestion (JSONLine)**: http://localhost:9428/insert/jsonline
- **Loki-compatible endpoint**: http://localhost:9428/select/logsql (for Grafana)

**Query Examples**:

```bash
# Insert a log
curl -X POST http://localhost:9428/insert/jsonline \
  -d '{"_msg":"test log","_time":"2025-10-19T10:00:00Z","_stream":"app","level":"info"}'

# Query logs
curl 'http://localhost:9428/select/logsql/query' \
  -d 'query=_stream:app | limit 10'
```

---

### Vector (Log Collection Agent)

Vector publishes **no ports**. Its GraphQL API (8686) is not enabled — `vector.yaml`
declares no `api:` block — so the published port was removed in #94 rather than left
open on 0.0.0.0 serving nothing.

#### Enabling the API (8686), if ever needed

Add an `api:` block to `vector.yaml` AND republish the port in `compose.yaml`; one
without the other does nothing. Only then do these become reachable:

- **Health Check**: http://localhost:8686/health
- **GraphQL Playground**: http://localhost:8686/playground
- **Metrics**: http://localhost:8686/metrics (Prometheus format)

**Use Cases**:

- Monitor Vector's performance and health
- Debug log collection issues
- Export Vector's internal metrics to VictoriaMetrics

---

### Grafana (Visualization)

| Port     | Protocol | Purpose      | Used By                  |
| -------- | -------- | ------------ | ------------------------ |
| **3000** | HTTP     | **Web UI**   | Users, Dashboard access  |

#### Port 3000 - Web Interface

- **Login**: http://localhost:3000
- **Dashboards**: http://localhost:3000/dashboards
- **Data Sources**: http://localhost:3000/datasources
- **Explore**: http://localhost:3000/explore

#### Outbound: SMTP 587 (alert notifications, #139)

Grafana's only outbound port. `GF_SMTP_HOST` is `smtp.gmail.com:587` with
`OpportunisticStartTLS`, templated into `.env` from the host's shared Gmail relay
credentials (`vault_watchtower_email_*` — the same relay watchtower uses). It
carries the four provisioned alert rules to the `homelab-email` contact point.

Nothing listens on 587 here; egress to it must stay open or alerting silently
stops delivering. Rules, routing and the shared-relay coupling:
[README.md → Alerting](README.md#alerting).

> Every Grafana **API** call by IP must send `-H 'Host: grafana.moutovkin.com'`.
> `grafana.ini` sets `enforce_domain = true`, so anything else gets a 301 to the
> configured domain. `/api/health` is the one exemption.

---

### Telegraf (Metrics Collector)

**No exposed ports** - Telegraf only makes outbound connections:

- Pushes to VictoriaMetrics: http://victoriametrics:8428/api/v1/write
- Collects from Docker socket: /var/run/docker.sock
- Scrapes SNMP devices: Network devices on your LAN
- Reads system metrics: /proc, /sys filesystems

---

## Quick Reference Matrix

| Service             | Web UI URL                        | Primary API | Health Check                        |
| ------------------- | --------------------------------- | ----------- | ----------------------------------- |
| **VictoriaMetrics** | http://localhost:8428/vmui        | :8428       | http://localhost:8428/health        |
| **VictoriaLogs**    | http://localhost:9428/select/vmui | :9428       | http://localhost:9428/health        |
| **Vector**          | _none — API not enabled_          | _none_      | `docker logs vector`                |
| **Grafana**         | http://localhost:3000             | :3000       | http://localhost:3000/api/health    |

---

## Firewall Configuration

### For Remote Access (Optional)

If accessing from other machines on your network, open these ports:

```bash
# VictoriaMetrics Web UI (metrics exploration)
sudo ufw allow 8428/tcp comment 'VictoriaMetrics HTTP API'

# InfluxDB raw line-protocol socket. NOT for Home Assistant (#133) and not
# something to open casually — it is unauthenticated on TCP *and* UDP, and this
# deployment deliberately RESTRICTS it with a scoped nftables table rather than
# opening it (see README → Firewall (:8089), #122).
sudo ufw allow 8089/tcp comment 'VictoriaMetrics InfluxDB'
sudo ufw allow 8089/udp comment 'VictoriaMetrics InfluxDB UDP'

# VictoriaLogs Web UI (log exploration)
sudo ufw allow 9428/tcp comment 'VictoriaLogs HTTP API'

# Grafana dashboards
sudo ufw allow 3000/tcp comment 'Grafana Web UI'
```

### Internal Only (Default - Recommended)

If only accessing from Docker host (localhost), **no firewall changes needed**. The ports are bound to `0.0.0.0` but can be restricted to `127.0.0.1` for extra security:

Change in `observability.yml`:

```yaml
ports:
  - "127.0.0.1:8428:8428"  # Only localhost can access
  - "0.0.0.0:8089:8089"    # left wide only because it is scoped by nftables
```

> Two corrections to the block above, kept as a warning rather than deleted:
> the file it names (`observability.yml`) no longer exists — the compose file is
> `files/compose.yaml` — and binding a service to `127.0.0.1` on this host
> **breaks NPM**, which proxies in from its own LXC. Exposure here is narrowed
> with scoped nftables (`roles/nft_scoped_fw`), never with a loopback bind. The
> old "Home Assistant needs network access" comment was also false: HA has never
> used this port (#133).

---

## Port Security Best Practices

1. **InfluxDB line-protocol port (8089)**:
   - ⚠️  **Unauthenticated write endpoint**, on TCP *and* UDP. `--httpAuth.*`
     guards :8428 only. This is a liability, not a convenience.
   - ⚠️  Restricted by the scoped nftables table `inet observability_fw` to the
     sources in `observability_firewall.ports[8089]` (#122). It is the ONLY thing
     standing between this port and the LAN.
   - ⚠️  No client writes to it today (#133). If HA export is ever set up, use the
     authenticated 8428 HTTP path instead — see above.

2. **HTTP APIs (8428, 9428)**:
   - ✅ **Authenticated since #88** (`--httpAuth.username/password`, mandatory —
     an empty value means "auth disabled", which is why the role asserts all four
     credentials non-empty before deploying). The line this replaces claimed "no
     authentication by default"; that has not been true since #88.
   - ⚠️  Add a reverse proxy with its own auth if exposing to the internet
   - ⚠️  Use VPN if accessing remotely

3. **Grafana (3000)**:
   - ✅ Has authentication (username/password from .env)
   - ✅ Safe to expose on local network
   - ⚠️  Use strong password
   - ⚠️  Enable HTTPS for internet access

4. **Vector API (8686)**:
   - ✅ Not enabled and not published (removed in #94) — no exposure

---

## Data Flow Diagram with Ports

```ascii
┌─────────────────────┐
│  Home Assistant     │
│  (Remote Machine)   │
└──────────┬──────────┘
           ┆
           ┆ NOT WIRED (#133): HA has no influxdb: block and
           ┆ has never sent a byte. If ever set up, it must
           ┆ target :8428 (InfluxDB v1 HTTP API, authenticated),
           ┆ NOT :8089 (raw line-protocol socket).
           ▼
┌─────────────────────────────────┐
│    VictoriaMetrics              │
│    :8428 (HTTP API, Web UI,     │──┐
│           InfluxDB v1 HTTP API) │  │
│    :8089 (InfluxDB raw socket,  │  │
│           enabled, unused)      │  │
│    :2003 / :4242 NOT ENABLED    │  │
└─────────────────────────────────┘  │
           ▲                         │
           │ Prometheus              │
           │ remote_write            │ Queries
           │                         │
┌──────────┴──────────┐              │
│     Telegraf        │              │
│  (SNMP, Docker,     │              │
│   System Metrics)   │              │
└─────────────────────┘              │
                                     │
┌─────────────────────┐              │
│  Docker Containers  │              │
│   (Log Sources)     │              │
└──────────┬──────────┘              │
           │                         │
           │ Logs via                │
           │ Docker socket           │
           ▼                         │
┌─────────────────────┐              │
│      Vector         │              │
│   (no ports)        │              │
└──────────┬──────────┘              │
           │                         │
           │ JSONLine                │
           │ HTTP POST               │
           ▼                         │
┌─────────────────────────────────┐  │
│    VictoriaLogs                 │  │
│    :9428 (HTTP API, Web UI)     │──┤
└─────────────────────────────────┘  │
                                     │
                                     │
                                     ▼
                            ┌────────────────┐
                            │    Grafana     │
                            │  :3000 (UI)    │
                            └────────────────┘
```

---

## Testing Port Connectivity

### From Docker Host

```bash
# Test VictoriaMetrics HTTP API
curl http://localhost:8428/health

# Test the InfluxDB raw line-protocol socket. nc is the right tool BECAUSE 8089
# is not HTTP. Note what this demonstrates: an unauthenticated write, accepted
# from anything the nftables scope lets through (#122).
echo "test_metric value=42" | nc localhost 8089

# Test VictoriaLogs
curl http://localhost:9428/health

# Test Vector (no API port — check the container instead)
docker logs --tail 20 vector

# Test Grafana
curl http://localhost:3000/api/health
```

### From Remote Machine (e.g., Home Assistant)

```bash
# Replace YOUR_DOCKER_HOST_IP with actual IP.

# InfluxDB v1 HTTP write — port 8428, authenticated. This is the path an
# InfluxDB HTTP client (including Home Assistant's integration) must use.
# 8089 does NOT answer HTTP: curl'ing it is meaningless, which is why the
# example that used to sit here could never have proved anything (#133).
curl -u "$VM_USER:$VM_PASS" -X POST \
  'http://YOUR_DOCKER_HOST_IP:8428/write?db=homeassistant' \
  --data-binary 'homeassistant,entity_id=test value=123'    # → 204

# Test VictoriaMetrics query (also authenticated since #88)
curl -u "$VM_USER:$VM_PASS" 'http://YOUR_DOCKER_HOST_IP:8428/api/v1/query?query=up'

# Test Grafana
curl http://YOUR_DOCKER_HOST_IP:3000/api/health
```

---

## Troubleshooting Port Issues

### Port Already in Use

```bash
# Check what's using a port
sudo lsof -i :8428
sudo netstat -tulpn | grep 8428

# Stop conflicting service
sudo systemctl stop <service>
```

### Can't Connect from Remote Machine

```bash
# Check if port is listening
netstat -tulpn | grep 8089

# Check firewall
sudo ufw status
sudo iptables -L -n | grep 8089

# Check Docker port mapping
docker port victoriametrics
```

### Home Assistant Can't Connect

**Check first: is there anything to connect?** As of the #133 diagnosis
(2026-08-20) Home Assistant has **no `influxdb:` block at all** and has never
written a metric here. "Cannot connect" is almost certainly "was never
configured".

1. **Confirm HA is even configured to export**:

   ```bash
   # On the HA VM, /config/configuration.yaml — an influxdb: block must exist.
   # There is no UI path; the integration is YAML-only.
   grep -rn -i influx /config/*.yaml
   ```

2. **Confirm whether the sink has EVER received a row** — this is the check that
   distinguishes "stopped" from "never started", and it is the only one that
   would have caught #133:

   ```bash
   curl -s -u "$VM_USER:$VM_PASS" \
     'http://192.168.25.15:8428/api/v1/query?query=vm_rows_inserted_total' | grep influx
   # type="influx" stuck at 0 => no InfluxDB client has ever written, ever.
   ```

3. **Test the write path HA actually uses** (HTTP on 8428, not 8089):

   ```bash
   # From the HA host
   curl -u "$VM_USER:$VM_PASS" -X POST \
     'http://192.168.25.15:8428/write?db=homeassistant' \
     --data-binary 'probe,src=manual value=1'          # → 204
   ```

4. **Check Home Assistant logs**:

   ```bash
   # In Home Assistant
   cat /config/home-assistant.log | grep influx
   ```

5. **Common issues**:
   - No `influxdb:` block in `configuration.yaml` at all (the #133 case)
   - Pointed at **8089**, which cannot answer an HTTP client — use 8428
   - Missing `username`/`password`: 8428 rejects unauthenticated writes with 401
   - Wrong IP address in Home Assistant config

---

## Performance Monitoring

Monitor port usage and connection stats:

```bash
# Active connections to VictoriaMetrics
netstat -an | grep :8428

# Connection rate
watch -n 1 'netstat -an | grep :8089 | wc -l'

# Check if ports are saturated
ss -s
```

---

**Last Updated**: August 20, 2026 — the Home Assistant / :8089 sections were
corrected against measured evidence from the #133 diagnosis, and 2003/4242 were
marked not-enabled. No runtime configuration was changed; #133 remains open.

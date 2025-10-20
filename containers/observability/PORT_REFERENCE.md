# Port Reference Guide

## VictoriaMetrics Observability Stack - Complete Port Mapping

### VictoriaMetrics (Time-Series Metrics Database)

| Port | Protocol | Purpose | Used By |
|------|----------|---------|---------|
| **8428** | HTTP | **Primary HTTP API and Web UI** (`/vmui`) | Grafana queries, Telegraf writes, Manual queries |
| **8089** | TCP/UDP | **InfluxDB Line Protocol** | Home Assistant, InfluxDB-compatible clients |
| **2003** | TCP/UDP | **Graphite Protocol** | Graphite-compatible monitoring tools |
| **4242** | TCP | **OpenTSDB Protocol** | OpenTSDB-compatible clients |

#### Port 8428 - Main HTTP API

- **Web UI**: http://localhost:8428/vmui
- **Health Check**: http://localhost:8428/health
- **Metrics Query**: http://localhost:8428/api/v1/query
- **Prometheus Remote Write**: http://localhost:8428/api/v1/write
- **Series Query**: http://localhost:8428/api/v1/series

#### Port 8089 - InfluxDB Protocol ⭐ **Recommended for Home Assistant**

- **Write Endpoint**: Automatically available (no path needed)
- **Why Use This**: 
  - Native Home Assistant integration
  - No authentication required
  - Simpler than Prometheus remote_write
  - Better performance than HTTP API
- **Home Assistant Config**:

  ```yaml
  influxdb:
    api_version: 1
    host: YOUR_DOCKER_HOST_IP
    port: 8089  # VictoriaMetrics InfluxDB port
  ```

#### Port 2003 - Graphite Protocol

- For legacy Graphite monitoring tools
- Supports both TCP and UDP
- Common in older monitoring setups

#### Port 4242 - OpenTSDB Protocol

- For OpenTSDB-compatible clients
- HTTP-based metric ingestion
- Used in some enterprise monitoring systems

---

### VictoriaLogs (Log Aggregation Database)

| Port | Protocol | Purpose | Used By |
|------|----------|---------|---------|
| **9428** | HTTP | **HTTP API and Web UI** (`/select/vmui`) | Grafana queries, Vector writes, Manual queries |

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

| Port | Protocol | Purpose | Used By |
|------|----------|---------|---------|
| **8686** | HTTP | **GraphQL API** | Monitoring, debugging, metrics export |

#### Port 8686 - GraphQL API

- **Health Check**: http://localhost:8686/health
- **GraphQL Playground**: http://localhost:8686/playground
- **Metrics**: http://localhost:8686/metrics (Prometheus format)

**Use Cases**:

- Monitor Vector's performance and health
- Debug log collection issues
- Export Vector's internal metrics to VictoriaMetrics

---

### Grafana (Visualization)

| Port | Protocol | Purpose | Used By |
|------|----------|---------|---------|
| **3000** | HTTP | **Web UI** | Users, Dashboard access |

#### Port 3000 - Web Interface

- **Login**: http://localhost:3000
- **Dashboards**: http://localhost:3000/dashboards
- **Data Sources**: http://localhost:3000/datasources
- **Explore**: http://localhost:3000/explore

---

### Telegraf (Metrics Collector)

**No exposed ports** - Telegraf only makes outbound connections:

- Pushes to VictoriaMetrics: http://victoriametrics:8428/api/v1/write
- Collects from Docker socket: /var/run/docker.sock
- Scrapes SNMP devices: Network devices on your LAN
- Reads system metrics: /proc, /sys filesystems

---

## Quick Reference Matrix

| Service | Web UI URL | Primary API | Health Check |
|---------|-----------|-------------|--------------|
| **VictoriaMetrics** | http://localhost:8428/vmui | :8428 | http://localhost:8428/health |
| **VictoriaLogs** | http://localhost:9428/select/vmui | :9428 | http://localhost:9428/health |
| **Vector** | http://localhost:8686/playground | :8686 | http://localhost:8686/health |
| **Grafana** | http://localhost:3000 | :3000 | http://localhost:3000/api/health |

---

## Firewall Configuration

### For Remote Access (Optional)

If accessing from other machines on your network, open these ports:

```bash
# VictoriaMetrics Web UI (metrics exploration)
sudo ufw allow 8428/tcp comment 'VictoriaMetrics HTTP API'

# InfluxDB protocol for Home Assistant
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
  - "0.0.0.0:8089:8089"    # Home Assistant needs network access
```

---

## Port Security Best Practices

1. **InfluxDB Port (8089)**:
   - ✅ Keep open if Home Assistant is on different machine
   - ✅ No authentication required (VictoriaMetrics trusts your network)
   - ⚠️  Put Home Assistant and VictoriaMetrics on same VLAN
   - ⚠️  Use firewall to restrict to Home Assistant IP only

2. **HTTP APIs (8428, 9428)**:
   - ✅ No authentication by default (simple for homelabs)
   - ⚠️  Add reverse proxy with auth if exposing to internet
   - ⚠️  Use VPN if accessing remotely

3. **Grafana (3000)**:
   - ✅ Has authentication (username/password from .env)
   - ✅ Safe to expose on local network
   - ⚠️  Use strong password
   - ⚠️  Enable HTTPS for internet access

4. **Vector API (8686)**:
   - ℹ️  Optional - only for debugging
   - ℹ️  Can be removed from exposed ports if not needed

---

## Data Flow Diagram with Ports

```ascii
┌─────────────────────┐
│  Home Assistant     │
│  (Remote Machine)   │
└──────────┬──────────┘
           │
           │ InfluxDB Protocol
           │ Port 8089 TCP/UDP
           ▼
┌─────────────────────────────────┐
│    VictoriaMetrics              │
│    :8428 (HTTP API, Web UI)     │──┐
│    :8089 (InfluxDB)             │  │
│    :2003 (Graphite)             │  │
│    :4242 (OpenTSDB)             │  │
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
│  :8686 (GraphQL)    │              │
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

# Test VictoriaMetrics InfluxDB port
echo "test_metric value=42" | nc localhost 8089

# Test VictoriaLogs
curl http://localhost:9428/health

# Test Vector GraphQL
curl http://localhost:8686/health

# Test Grafana
curl http://localhost:3000/api/health
```

### From Remote Machine (e.g., Home Assistant)

```bash
# Replace YOUR_DOCKER_HOST_IP with actual IP
# Test InfluxDB write (Home Assistant uses this)
curl -X POST http://YOUR_DOCKER_HOST_IP:8089/write \
  -d 'homeassistant,entity_id=test value=123'

# Test VictoriaMetrics query
curl 'http://YOUR_DOCKER_HOST_IP:8428/api/v1/query?query=up'

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

1. **Verify VictoriaMetrics is running**:

   ```bash
   docker logs victoriametrics
   curl http://localhost:8428/health
   ```

2. **Test InfluxDB port from Home Assistant machine**:

   ```bash
   # From Home Assistant host
   telnet YOUR_DOCKER_HOST_IP 8089
   ```

3. **Check Home Assistant logs**:

   ```bash
   # In Home Assistant
   cat /config/home-assistant.log | grep influx
   ```

4. **Common issues**:
   - Wrong IP address in Home Assistant config
   - Firewall blocking port 8089
   - Docker not exposing port correctly
   - Network isolation between containers

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

**Last Updated**: October 19, 2025

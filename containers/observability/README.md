# Observability Stack

A comprehensive, **fully open-source** monitoring and observability stack for homelabs using the **VictoriaMetrics ecosystem**:

- **VictoriaMetrics** for metrics storage
- **VictoriaLogs** for log aggregation

_Researched with Grok 4 Fast and Claude Sonnet 4.5 at the end of October 2025, to understand the best combination of solutions for my homelab needs, i.e. long term Home Assistant time series data, alerts and logs aggregation from all containers and VMs and devices._

## Why VictoriaMetrics Ecosystem?

After evaluating various solutions, the VictoriaMetrics ecosystem was chosen for several key reasons:

### VictoriaMetrics (Metrics)

- **🔓 Fully Open Source**: Apache 2.0 license, no risk of closed-source transitions (unlike InfluxDB 3.x)
- **📊 Superior Performance**: 10-20x better compression than InfluxDB, faster queries
- **💾 Lower Resource Usage**: Uses 50% less memory and disk space than InfluxDB
- **🔋 Long-term Storage**: Optimized for multi-year retention (perfect for Home Assistant sensor data)
- **📈 Prometheus Compatible**: Drop-in replacement for Prometheus with better performance
- **🚀 Single Binary**: No complex setup, no external dependencies, no manual CLI initialization
- **🔍 Native PromQL & MetricsQL**: Familiar query language with powerful enhancements
- **📱 Built-in Web UI**: Instant data exploration at `/vmui` endpoint

### VictoriaLogs (Logs)

- **🔓 Fully Open Source**: Apache 2.0 license
- **⚡ 10x Faster**: Than Loki for log ingestion and queries
- **💾 5x Less Disk Space**: Superior compression compared to Loki
- **🧠 Minimal Memory**: ~100-200MB vs Loki's 500MB+
- **🔍 LogsQL**: Powerful query language (more intuitive than LogQL)
- **📱 Built-in Web UI**: Explore logs at `/select/vmui` endpoint
- **🚀 Zero Config**: Works out of the box, no complex YAML configurations

## Architecture

```ascii
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  Home Assistant  │───▶│ VictoriaMetrics  │◀───│    Telegraf      │
│  (IoT Sensors)   │    │    (Metrics)     │    │ (SNMP, System)   │
└──────────────────┘    └──────────────────┘    └──────────────────┘
                                │
                                │
┌──────────────────┐    ┌──────────────────┐
│ Docker Containers│───▶│  VictoriaLogs    │
│    (Logs via     │    │     (Logs)       │
│      Vector)     │    └──────────────────┘
└──────────────────┘            │
         │                      │
         └──────────┬───────────┘
                    ▼
            ┌───────────────┐
            │   Grafana     │
            │(Visualization)│
            └───────────────┘
```

## Components

| Service | Ports | Purpose |
|---------|-------|---------|
| **VictoriaMetrics** | 8428 (HTTP/UI)<br>8089 (InfluxDB)<br>2003 (Graphite)<br>4242 (OpenTSDB) | Time-series database for metrics storage |
| **VictoriaLogs** | 9428 (HTTP/UI) | High-performance log aggregation and storage |
| **Vector** | 8686 (GraphQL API) | High-performance log collection and routing |
| **Telegraf** | - | Metrics collection (SNMP, Docker, system) |
| **Grafana** | 3000 (Web UI) | Unified visualization dashboard |

## Deployment

### Prerequisites

1. **Docker and Docker Compose** installed on server
2. **Ports available**: 3000, 8428, 8089, 9428 (see PORT_REFERENCE.md)
3. **Minimum 8GB RAM** recommended for full stack

### Deployment Steps

#### 1. Edit Environment Variables

```bash
cp .env.example .env
nano .env

# Required: Set Grafana password (generate with: openssl rand -hex 32):
GRAFANA_PASSWORD=<your-password>

# Optional: Enable VictoriaMetrics/VictoriaLogs authentication:
# VM_AUTH_USERNAME=obs
# VM_AUTH_PASSWORD=<your-password>
# VL_AUTH_USERNAME=obs
# VL_AUTH_PASSWORD=<your-password>

# Required: Docker group ID (for Telegraf to access Docker socket):
DOCKER_GID=999                    # Run: getent group docker | cut -d: -f3

# Optional: Network monitoring (SNMP disabled by default - requires MIB files):
# ROUTER_IP=192.168.1.1
# SNMP_COMMUNITY=public

# System configuration:
TIMEZONE=America/Los_Angeles      # Your timezone
```

#### 2. Copy Configuration to Server

```bash
# Copy data directory to server's /data
scp -r data/* user@server:/data/

# Copy Docker Compose and environment files
scp observability.yml .env user@server:~/observability/
```

**Or use rsync (recommended for updates):**

```bash
rsync -avz --progress data/* user@server:/data/
rsync -avz observability.yml .env user@server:~/observability/
```

#### 3. Set Permissions on Server

```bash
ssh user@server

# Set correct ownership
sudo chown -R 472:472 /data/grafana       # Grafana runs as UID 472
sudo chown -R $USER:$USER /data/victoriametrics
sudo chown -R $USER:$USER /data/victorialogs
sudo chown -R $USER:$USER /data/vector
sudo chown -R $USER:$USER /data/telegraf

# Set permissions
sudo chmod -R 755 /data
```

#### 4. Deploy Stack

```bash
cd ~/observability

# Start all services
docker compose -f observability.yml up -d

# Verify deployment
docker compose -f observability.yml ps
docker stats

# Check logs
docker compose -f observability.yml logs -f
```

#### 5. Access Services

- **Grafana**: http://server-ip:3000 (login with .env credentials)
- **VictoriaMetrics UI**: http://server-ip:8428/vmui
- **VictoriaLogs UI**: http://server-ip:9428/select/vmui

#### 6. Initial Configuration

#### VictoriaMetrics Setup

**VictoriaMetrics requires NO initial setup** - it's ready to receive data immediately after deployment! 🎉

1. **Verify it's running:**

   ```bash
   curl http://localhost:8428/health
   # Should return: "OK"
   ```

2. **Check metrics:**

   ```bash
   # View all metric names
   curl http://localhost:8428/api/v1/labels

   # Query a specific metric
   curl 'http://localhost:8428/api/v1/query?query=up'
   ```

3. **Open the Web UI:**
   - URL: <http://localhost:8428/vmui>
   - **No login required!**
   - Use **MetricsQL** or **PromQL** to query data
   - Explore all available metrics and labels
   - Create ad-hoc graphs and dashboards

4. **Test write endpoint:**

   ```bash
   # VictoriaMetrics accepts metrics via InfluxDB line protocol
   curl -X POST http://localhost:8428/write -d 'test_metric value=42'
   ```

#### VictoriaLogs Setup

**VictoriaLogs also requires NO initial setup** - it's ready to receive logs immediately! 🎉

1. **Verify it's running:**

   ```bash
   curl http://localhost:9428/health
   # Should return: OK
   ```

2. **Open the Web UI:**
   - URL: <http://localhost:9428/select/vmui>
   - **No login required!**
   - Use **LogsQL** to query logs
   - Explore all log streams and fields

3. **Test log ingestion:**

   ```bash
   # Send a test log
   curl -X POST http://localhost:9428/insert/jsonline \
     -d '{"_msg":"test log message","_time":"'$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)'","_stream":"test","level":"info"}'
   ```

4. **Query logs:**

   ```bash
   # Get recent logs
   curl 'http://localhost:9428/select/logsql/query' \
     -d 'query=_stream:test | limit 10'
   ```

#### Grafana Setup

1. **Open Grafana:** <http://localhost:3000>

2. **Login** with credentials from `.env`

3. **Add VictoriaMetrics data source:**
   - Go to: **Configuration** → **Data Sources** → **Add data source**
   - Select: **Prometheus**
   - **Name:** VictoriaMetrics
   - **URL:** `http://victoriametrics:8428`
   - **Access:** Server (default)
   - Click **Save & Test**

4. **Add VictoriaLogs data source:**
   - Go to: **Configuration** → **Data Sources** → **Add data source**
   - Select: **Loki** (VictoriaLogs is Loki-compatible!)
   - **Name:** VictoriaLogs
   - **URL:** `http://victorialogs:9428/select/logsql`
   - **Access:** Server (default)
   - Click **Save & Test**

## Data Sources & Use Cases

### VictoriaMetrics - Time-Series Metrics

- **Home Assistant sensors** (temperature, humidity, power, energy usage)
- **System metrics** via Telegraf (CPU, memory, disk, network)
- **SNMP device stats** (routers, switches, UPS, access points)
- **Docker container metrics** (resource usage per container)
- **Custom application metrics** (Prometheus format)

### VictoriaMetrics Query Examples (PromQL/MetricsQL)

#### Query Home Assistant sensor data

```promql
# Current temperature in living room
sensor_temperature{entity_id="living_room"}

# Temperature over last 24 hours
sensor_temperature{entity_id="living_room"}[24h]

# Average power consumption per hour
avg_over_time(sensor_power_watts[1h])

# Current humidity in all rooms
sensor_humidity{entity_id=~".*_room"}

# Daily energy consumption
sum(increase(sensor_energy_kwh[1d]))
```

#### Query system metrics

```promql
# CPU usage percentage
100 - (avg by (host) (rate(cpu_usage_idle[5m])) * 100)

# Memory usage percentage
100 * (1 - (mem_available / mem_total))

# Disk usage percentage
100 * (disk_used / disk_total)

# Network bandwidth (bytes/sec)
rate(net_bytes_recv[5m])
rate(net_bytes_sent[5m])
```

#### Network device stats (SNMP)

```promql
# Interface bandwidth (convert to bits/sec)
rate(ifInOctets[5m]) * 8
rate(ifOutOctets[5m]) * 8

# Packet errors per second
rate(ifInErrors[5m])

# Device uptime (in days)
sysUpTime / 8640000
```

### VictoriaLogs - Log Aggregation

- **Container logs** from all Docker services
- **System logs** from host machine
- **Application-specific logs** (PostgreSQL, Vaultwarden, etc.)

### VictoriaLogs Query Examples (LogsQL)

LogsQL is more intuitive than LogQL! Here are some examples:

```logsql
# All logs from a specific container
_stream:container_name:postgresql

# Error logs across all containers
_msg:~"error|ERROR|Error"

# Logs from last hour with keyword
_time:>1h AND _stream:container_name:grafana AND _msg:"authentication"

# Count errors per container
stats by (_stream) count() logs | filter _msg:~"error"

# Top 10 most common error messages
stats by (_msg) count() logs
| filter _msg:~"error"
| sort by (count) desc
| limit 10
```

**LogsQL Advantages:**

- More intuitive syntax than LogQL
- Faster queries (10x faster than Loki)
- Better full-text search capabilities
- SQL-like aggregations and statistics

## Configuration

### Telegraf Configuration

Edit `telegraf/telegraf.conf` to customize:

#### System Metrics

The following inputs are enabled by default:
- **CPU, Memory, Disk, Network** - System resource monitoring
- **Docker** - Container metrics (requires `DOCKER_GID` in `.env`)
- **Ping** - Internet connectivity checks (8.8.8.8, 1.1.1.1)
- **HTTP Response** - Service health checks

#### SNMP Monitoring (Disabled by Default)

SNMP monitoring is commented out because it requires MIB files not included in the Telegraf container.

To enable SNMP monitoring:

1. **Uncomment the SNMP section** in `telegraf/telegraf.conf`
2. **Set environment variables** in `.env`:
   ```bash
   ROUTER_IP=192.168.1.1
   SNMP_COMMUNITY=public
   ```
3. **Restart Telegraf**

Example (already in config, just uncommented):
```toml
# [[inputs.snmp]]
#   agents = ["${ROUTER_IP}"]
#   version = 2
#   community = "${SNMP_COMMUNITY}"
#   # ... additional configuration
```

## Home Assistant Integration

### Option 1: InfluxDB Integration (Recommended) ⭐

VictoriaMetrics supports the InfluxDB line protocol natively on **port 8089**, so you can use Home Assistant's built-in InfluxDB integration with **zero additional configuration**!

1. **In Home Assistant**, add to `configuration.yaml`:

   ```yaml
   influxdb:
     api_version: 1  # Use InfluxDB v1 protocol (simpler!)
     host: YOUR_DOCKER_HOST_IP  # e.g., 192.168.1.100
     port: 8089  # VictoriaMetrics InfluxDB port
     # No database, username, or password needed!

     # Optional: Override measurement name (default is "state")
     default_measurement: homeassistant

     # Configure which sensors to track
     include:
       entities:
         # Temperature sensors
         - sensor.living_room_temperature
         - sensor.bedroom_temperature
         - sensor.outdoor_temperature

         # Humidity sensors
         - sensor.living_room_humidity
         - sensor.bedroom_humidity

         # Power/Energy monitoring
         - sensor.power_consumption
         - sensor.solar_production
         - sensor.total_energy_cost

         # Climate devices
         - climate.thermostat
         - climate.heat_pump

         # Add all sensors you want long-term tracking for

     # Exclude non-numeric or frequently-changing sensors
     exclude:
       entities:
         - sensor.last_boot
         - sensor.ip_address
         - sensor.external_url
   ```

2. **Restart Home Assistant**

3. **Verify data in VictoriaMetrics:**

   ```bash
   # Check metrics are being received
   curl 'http://localhost:8428/api/v1/query?query={__name__=~".*temperature.*"}'

   # Or open the Web UI
   open http://localhost:8428/vmui
   # Search for: sensor_temperature
   ```

4. **Query your data in Grafana:**

   ```promql
   # Temperature in living room
   homeassistant{entity_id="sensor.living_room_temperature"}

   # Average temperature over 24 hours
   avg_over_time(homeassistant{entity_id="sensor.living_room_temperature"}[24h])
   ```

### Why InfluxDB Protocol is Best

- ✅ **Native support**: VictoriaMetrics has built-in InfluxDB compatibility
- ✅ **Zero config**: No authentication, databases, or tokens needed
- ✅ **Better performance**: Direct write, no intermediate processing
- ✅ **Simpler setup**: Just change host and port in Home Assistant
- ✅ **Proven**: Used by thousands of Home Assistant users

### Alternative Protocols

VictoriaMetrics also supports:

- **Graphite** (port 2003): For legacy monitoring tools
- **OpenTSDB** (port 4242): For OpenTSDB-compatible clients
- **Prometheus remote_write**: Via Telegraf or vmagent

### Option 2: Telegraf HTTP Plugin

## Data Retention Strategy

- **VictoriaMetrics**: **5 years** (configured in docker-compose with `--retentionPeriod=5y`)
  - Optimized for long-term Home Assistant sensor data
  - Automatic compression reduces disk usage dramatically
  - No separate "buckets" or "databases" - all metrics in one store
  - Efficient storage: ~10-20x better than InfluxDB

- **VictoriaLogs**: **90 days** (configured in docker-compose with `--retentionPeriod=90d`)
  - Time-based retention for log data
  - Automatic compression (5x better than Loki)
  - Shorter retention is reasonable for logs vs metrics
  - Can be extended if needed (e.g., `--retentionPeriod=180d` for 6 months)

To change retention:

```bash
# Edit observability.yml for VictoriaMetrics:
--retentionPeriod=10y  # 10 years

# Edit observability.yml for VictoriaLogs:
--retentionPeriod=180d  # 180 days
```

## Grafana Dashboards

### Recommended Dashboards

Import these dashboards from <https://grafana.com/dashboards>:

#### VictoriaMetrics & Prometheus-Compatible

- **VictoriaMetrics**: ID 10229, 11176
- **VictoriaMetrics Cluster**: ID 11831
- **Node Exporter Full**: ID 1860 (for system metrics)
- **Docker Monitoring**: ID 893, 15120

#### Home Assistant

- **Home Assistant**: ID 12545 (requires minor modifications for VictoriaMetrics/PromQL)
- Create custom dashboards for your specific sensors using PromQL queries

#### Network

- **SNMP Interface Stats**: ID 1124
- **SNMP Network Performance**: ID 11169
- **UniFi Poller**: ID 11315 (if using UniFi network devices)

### Creating Custom Dashboards

1. **Use PromQL for metrics queries:**

   ```promql
   # Example: Average temperature by room
   avg by (entity_id) (sensor_temperature)
   ```

2. **Use LogQL for log panels:**

   ```logql
   {container="your-container"} |= "error"
   ```

3. **Combine metrics and logs** in a single dashboard for full observability

## Alerting

### Grafana Alerts

Configure alerts for:

- **High CPU/Memory usage** (>80% for 5 minutes)
- **Disk space low** (<10% remaining)
- **Container down** (health check failed)
- **Network device offline** (SNMP timeout)
- **Temperature extremes** (outside normal range)

### Notification Channels

- Email notifications
- Slack/Discord webhooks
- Home Assistant notifications

## Performance Optimization

### VictoriaMetrics

VictoriaMetrics is **extremely efficient** out of the box:

- **Memory usage**: ~200-500MB for typical homelab (vs 1-2GB for InfluxDB)
- **Disk usage**: 10-20x better compression than InfluxDB
- **Query speed**: Sub-second queries even with years of data
- **No tuning needed**: Works great with default settings

Current settings optimized for 16GB RAM server:

```yaml
--memory.allowedPercent=70      # Use up to 70% available memory
--search.maxConcurrentRequests=12  # Parallel query limit
```

### VictoriaLogs

VictoriaLogs is also **extremely efficient**:

- **Memory usage**: ~100-200MB (vs Loki's 500MB+)
- **Disk usage**: 5x better compression than Loki
- **Ingestion speed**: 10x faster than Loki
- **Query speed**: 10x faster than Loki for full-text search
- **No tuning needed**: Works perfectly with defaults

### Vector

- **Memory usage**: ~50-100MB (ultra-efficient Rust-based)
- **Disk buffering**: 256MB buffer for network issues
- **High throughput**: Can handle 10GB+/day of logs easily

### Telegraf

- **Collection interval**: 60s (configurable in `telegraf.conf`)
- **Batch size**: 1000 metrics per write
- **Buffer**: 10000 metrics (handles temporary network issues)

## Troubleshooting

### Common Issues

#### VictoriaMetrics not receiving data

1. **Check Telegraf logs:**

   ```bash
   docker logs telegraf
   ```

2. **Verify VictoriaMetrics API:**

   ```bash
   curl http://localhost:8428/api/v1/labels
   ```

3. **Check network connectivity:**

   ```bash
   docker exec telegraf ping victoriametrics
   ```

4. **Test write endpoint:**

   ```bash
   curl -X POST http://localhost:8428/write -d 'test_metric value=123'
   ```

#### Grafana can't connect to VictoriaMetrics

1. **Verify data source URL:** Should be `http://victoriametrics:8428` (not localhost)
2. **Check Docker network:** Both should be on `observability_network`
3. **Test from Grafana container:**

   ```bash
   docker exec grafana curl http://victoriametrics:8428/health
   ```

#### Missing logs in Loki

```bash
# Check Promtail status
docker logs promtail

# Verify log file permissions
ls -la /var/lib/docker/containers/
```

#### High resource usage

```bash
# Monitor container resources
docker stats

# Check disk usage by containers
docker system df -v
```

### Health Checks

All services include health checks. Monitor status with:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

## Security Considerations

- **Change default passwords** in `.env` file
- **Use strong tokens** for InfluxDB and Grafana
- **Restrict network access** if exposed publicly
- **Regular updates** via Watchtower or manual updates
- **Monitor access logs** in Grafana

## Integration Examples

### Proxmox Integration

```toml
# Add to telegraf.conf
[[inputs.http]]
  urls = ["https://proxmox:8006/api2/json/nodes/pve/status"]
  # Configure authentication
```

### Docker Swarm Integration

```toml
# Add to telegraf.conf for swarm clusters
[[inputs.docker]]
  endpoint = "unix:///var/run/docker.sock"
  gather_services = true
```

### Custom Application Metrics

```toml
# Monitor custom applications
[[inputs.http]]
  urls = ["http://myapp:8080/metrics"]
  data_format = "prometheus"
```

---

## Why Victoria* Ecosystem vs Alternatives?

### VictoriaMetrics vs InfluxDB 3.x

| Feature | VictoriaMetrics | InfluxDB 3.x (Closed Source) |
|---------|-----------------|------------------------------|
| **License** | ✅ Apache 2.0 (Open Source) | ❌ Proprietary (Closed Source) |
| **Disk Usage** | ✅ 1x (best compression) | ❌ 10-15x larger |
| **Memory** | ✅ Low (~200-500MB) | ❌ High (~1-2GB+) |
| **Query Language** | ✅ PromQL/MetricsQL | SQL (limited) |
| **Setup Complexity** | ✅ None (instant) | ❌ Manual CLI setup required |
| **Web UI** | ✅ Built-in (`/vmui`) | ❌ Separate container needed |
| **Query Speed** | ✅ Sub-second | Slower |
| **Community** | ✅ Active & growing | Declining after v3 changes |
| **Long-term Support** | ✅ Guaranteed (open source) | ⚠️  Uncertain (proprietary) |
| **Home Assistant** | ✅ Native InfluxDB protocol | ✅ InfluxDB v2 API |
| **Telegraf Support** | ✅ Prometheus remote write | ✅ InfluxDB output |
| **Resource Efficiency** | ✅ Best-in-class | Average |

### VictoriaLogs vs Loki

| Feature | VictoriaLogs | Loki |
|---------|--------------|------|
| **License** | ✅ Apache 2.0 (Open Source) | ✅ Apache 2.0 (Open Source) |
| **Disk Usage** | ✅ 1x (best compression) | ❌ 5x larger |
| **Memory** | ✅ Very Low (~100-200MB) | ⚠️  Medium (~500MB+) |
| **Ingestion Speed** | ✅ 10x faster | Slower |
| **Query Speed** | ✅ 10x faster | Slower |
| **Query Language** | ✅ LogsQL (intuitive) | ⚠️  LogQL (complex) |
| **Setup Complexity** | ✅ Zero config | ❌ Complex YAML configs |
| **Web UI** | ✅ Built-in (`/select/vmui`) | ❌ Needs Grafana |
| **Full-text Search** | ✅ Excellent | ⚠️  Limited |
| **Log Agent** | Vector (Rust, fast) | Promtail (Go) |
| **Resource Efficiency** | ✅ Best-in-class | Good |

**Bottom Line**: VictoriaMetrics + VictoriaLogs provide better performance, lower resource usage, and stay fully open source - perfect for homelabs!

## Resources

- [VictoriaMetrics Documentation](https://docs.victoriametrics.com/)
- [VictoriaMetrics Quick Start](https://docs.victoriametrics.com/Quick-Start.html)
- [MetricsQL Reference](https://docs.victoriametrics.com/MetricsQL.html)
- [VictoriaLogs Documentation](https://docs.victoriametrics.com/VictoriaLogs/)
- [LogsQL Reference](https://docs.victoriametrics.com/VictoriaLogs/LogsQL.html)
- [Vector Documentation](https://vector.dev/docs/)
- [Grafana Documentation](https://grafana.com/docs/grafana/latest/)
- [Telegraf Documentation](https://docs.influxdata.com/telegraf/v1/)

## Support

For issues or questions:

1. **VictoriaMetrics**: [GitHub Issues](https://github.com/VictoriaMetrics/VictoriaMetrics/issues) | [Community](https://victoriametrics.com/community/)
2. **VictoriaLogs**: [GitHub Issues](https://github.com/VictoriaMetrics/VictoriaMetrics/issues) (same repo)
3. **Grafana**: [Community Forums](https://community.grafana.com/)
4. **Vector**: [Discord](https://chat.vector.dev/) | [GitHub](https://github.com/vectordotdev/vector)
5. **Telegraf**: [Community Forums](https://community.influxdata.com/)

---

**Note**: This stack uses the fully open-source VictoriaMetrics ecosystem for both metrics (VictoriaMetrics) and logs (VictoriaLogs). This provides superior performance, lower resource usage, and no risk of closed-source licensing changes.

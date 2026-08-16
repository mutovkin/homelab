# Grafana Dashboards Directory

Place your Grafana dashboard JSON files in this directory.

## Directory Structure

```
dashboards/
├── system/           # System metrics dashboards
├── docker/           # Docker container dashboards
├── network/          # Network monitoring dashboards
└── README.md         # This file
```

## Adding Dashboards

### Option 1: Export from Grafana UI
1. Create/edit dashboard in Grafana
2. Share → Export → Save to file
3. Copy JSON file to this directory

### Option 2: Import from Grafana.com
1. Browse https://grafana.com/grafana/dashboards/
2. Download JSON
3. Copy to this directory

### Recommended Dashboards

#### VictoriaMetrics
- **VictoriaMetrics - Single Node**: https://grafana.com/grafana/dashboards/10229
- **VictoriaMetrics Cluster**: https://grafana.com/grafana/dashboards/11176

#### Docker
- **Docker Container & Host Metrics**: https://grafana.com/grafana/dashboards/10619
- **Docker Monitoring**: https://grafana.com/grafana/dashboards/893

#### System
- **Node Exporter Full**: https://grafana.com/grafana/dashboards/1860
- **System Metrics (Telegraf)**: https://grafana.com/grafana/dashboards/928

#### Network
- **SNMP Interface Details**: https://grafana.com/grafana/dashboards/11169
- **Network Traffic**: https://grafana.com/grafana/dashboards/12314

## Auto-Loading

Dashboards in this directory are automatically loaded by Grafana on startup.
Changes are detected every 30 seconds (configurable in `provisioning/dashboards/dashboards.yaml`).

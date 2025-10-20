# Observability Stack Data Directory

This directory structure matches the expected `/data` layout on the server.

## Structure

```
data/
├── victoriametrics/      # VictoriaMetrics data (empty - auto-populated by container)
├── victorialogs/         # VictoriaLogs data (empty - auto-populated by container)
├── vector/
│   └── vector.yaml       # Vector log collector configuration
├── telegraf/
│   └── telegraf.conf     # Telegraf metrics collector configuration
└── grafana/
    ├── data/             # Grafana persistent data (empty - auto-populated)
    ├── config/
    │   └── grafana.ini   # Grafana custom configuration
    ├── dashboards/
    │   └── README.md     # Dashboard installation guide
    └── provisioning/
        ├── datasources/
        │   └── datasources.yaml   # Auto-configured datasources
        └── dashboards/
            └── dashboards.yaml    # Dashboard auto-loading config
```

## Deployment to Server

### Option 1: SCP entire data directory
```bash
# From your local machine
cd /Users/surge/dev/homelab/containers/observability
scp -r data/* user@server:/data/
```

### Option 2: Rsync (better for updates)
```bash
rsync -avz --progress data/* user@server:/data/
```

### After copying to server

```bash
# SSH to server
ssh user@server

# Set correct permissions
sudo chown -R 472:472 /data/grafana       # Grafana runs as UID 472
sudo chown -R $USER:$USER /data/victoriametrics
sudo chown -R $USER:$USER /data/victorialogs
sudo chown -R $USER:$USER /data/vector
sudo chown -R $USER:$USER /data/telegraf

# Deploy the stack
cd /path/to/observability
docker compose -f observability.yml up -d
```

## Notes

- The `victoriametrics/`, `victorialogs/`, and `grafana/data/` directories are empty
  - They will be auto-populated by the containers
  - Don't put files in them before first run

- Configuration files are read-only (`:ro` mount in docker-compose)
  - To update: modify locally, re-SCP, restart container

- The `observability.yml` and `.env` files should be in the parent directory
  - Not inside the `/data` folder
  - Keep them in your deployment directory on the server

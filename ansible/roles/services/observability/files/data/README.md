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

**Deploy these configs with Ansible, not by hand.** The `observability` role copies
`vector/vector.yaml`, `telegraf/telegraf.conf` and `grafana/config/grafana.ini` to
`/data/...`, syncs `grafana/provisioning/`, and restarts only the services whose
config actually changed:

```bash
task deploy:service -- --tags observability --limit eq12_docker
```

Preview first with `--check --diff`. Editing a file here and re-running the deploy is
the whole workflow — there is no copy step to remember.

Copying by hand (`scp`/`rsync` into `/data`) is out of policy: this repo's Critical
Rule 1 is that systems change through Ansible, never ad-hoc SSH. Hand-copied config
also drifts silently from the repo, which is exactly how a broken Vector config went
unnoticed for a month (issue #73).

The container-facing directories `victoriametrics/`, `victorialogs/` and
`grafana/data/` are runtime state — the role creates them empty and never overwrites
them.

## Notes

- The `victoriametrics/`, `victorialogs/`, and `grafana/data/` directories are empty
  - They will be auto-populated by the containers
  - Don't put files in them before first run

- Configuration files are read-only (`:ro` mount in docker-compose)
  - To update: edit the file here, then re-run the Ansible deploy — the role copies it
    and restarts the affected service automatically

- The `observability.yml` and `.env` files should be in the parent directory
  - Not inside the `/data` folder
  - Keep them in your deployment directory on the server

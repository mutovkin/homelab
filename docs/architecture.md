# Architecture

## Machine Topology

```ascii
                         LAN (192.168.x.x)
                    ┌───────────┴───────────┐
                    │                       │
            ┌───────┴───────┐       ┌───────┴───────┐
            │  Beelink EQ12 │       │ Minisforum N5 │
            │  Proxmox (pve)│       │Proxmox (n5pro)│
            │  16GB / N100  │       │ 96GB / AMD    │
            └───────┬───────┘       └───────┬───────┘
                    │                       │
        ┌───────────┼───────────┐       ┌───┼───────────┐
        │           │           │       │   │           │
   ┌────┴────┐ ┌────┴─────┐ ┌───┴───┐  ┌┴───┴──┐  ┌─────┴───┐
   │ VM-100  │ │ CT-101   │ │CT-104 │  │VM-200 │  │ CT-201  │
   │ Home    │ │deb-docker│ │ NPM   │  │TrueNAS│  │n5-docker│
   │Assistant│ │(Docker)  │ │       │  │       │  │(Docker) │
   └─────────┘ └────┬─────┘ └───────┘  └───────┘  └────┬────┘
                    │                                   │
    ┌───────┬───────┼───────┬──────┬──────┬──────┐     │
    │       │       │       │      │      │      │     ├──Immich
   PG    Obs    Vault    SearX  Joplin  Port  Watch   ├──Frigate
                                                       ├──NextCloud
                                                       ├──PG
                                                       ├──Port
                                                       └──Watch
```

## Automation Layers

```ascii
┌──────────────────────────────────────────────────────────────┐
│  Layer 2: Docker Compose                                     │
│  Service definitions in containers/                          │
│  Deployed by Ansible service roles                           │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Ansible                                            │
│  Proxmox host OS config + VM/LXC provisioning                │
│  Guest configuration (Docker) + service deployment           │
│  Defined in ansible/ (roles: proxmox_host, proxmox_guests,   │
│  docker_host, services/*)                                    │
└──────────────────────────────────────────────────────────────┘
```

### Orchestration

```bash
# Full deployment (single command):
task deploy:full
# Equivalent to: ansible-playbook ansible/playbooks/site.yml

# Or step by step:
task infra:hosts      # 1. Configure Proxmox OS + provision VMs/LXCs
task infra:guests     # 2. Configure guests (Docker, packages)
task deploy:services  # 3. Deploy compose stacks
```

## Docker Network Map

### EQ12 — CT 101 (deb-docker)

All Docker networks use 172.x.x.x subnets to avoid conflicts with the LAN (192.168.x.x).

| Service Stack | Network Name | Subnet | Gateway |
|---|---|---|---|
| Observability | `observability_network` | 172.20.0.0/24 | 172.20.0.1 |
| PostgreSQL | `postgres_network` | 172.21.0.0/24 | 172.21.0.1 |
| SearXNG | `searxng_network` | 172.22.0.0/24 | 172.22.0.1 |
| Portainer | `default` | 172.23.0.0/24 | 172.23.0.1 |
| Vaultwarden | `vaultwarden_network` | 172.24.0.0/24 | 172.24.0.1 |
| Watchtower | `watchtower_network` | 172.25.0.0/24 | 172.25.0.1 |
| Joplin | `postgres_network` (shared) | 172.21.0.0/24 | 172.21.0.1 |

### N5 Pro — CT 201 (n5-docker)

| Service Stack | Network Name | Subnet | Gateway |
|---|---|---|---|
| PostgreSQL | `postgres_network` | 172.30.0.0/24 | 172.30.0.1 |
| Immich | `immich_network` | 172.31.0.0/24 | 172.31.0.1 |
| Frigate | `frigate_network` | 172.32.0.0/24 | 172.32.0.1 |
| NextCloud | `nextcloud_network` | 172.33.0.0/24 | 172.33.0.1 |
| Portainer | `portainer_network` | 172.34.0.0/24 | 172.34.0.1 |
| Watchtower | `watchtower_network` | 172.35.0.0/24 | 172.35.0.1 |
| Immich → PG | `postgres_network` (shared) | 172.30.0.0/24 | 172.30.0.1 |
| NextCloud → PG | `postgres_network` (shared) | 172.30.0.0/24 | 172.30.0.1 |

## Cross-Host Communication

- **Method**: Direct LAN (both machines on the same 192.168.x.x network)
- **Use cases**:
  - Centralized monitoring: Telegraf on N5 Pro → VictoriaMetrics on EQ12 (or vice versa)
  - NFS: N5 Pro Docker LXC → TrueNAS VM for Frigate recordings and media storage
- **Configuration**: LAN IPs templated into `.env` files by Ansible using inventory variables

## GPU Device Passthrough (N5 Pro)

The N5 Pro Docker LXC (CT-201) has `/dev/dri` device passthrough configured via the
`proxmox_guests` role. This provides VAAPI hardware acceleration for:
- **Frigate**: Hardware video decoding of camera streams
- **Immich**: Video transcoding and ML inference acceleration

This is device sharing (not full PCI passthrough), configured via LXC config entries:
```
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
```

The TrueNAS VM (VM-200) has full PCI passthrough of the JMicron SATA controller
for direct access to 5× 26TB HDDs.

## Service Placement

| Host | Docker Host | Services |
|---|---|---|
| EQ12 | CT 101 (deb-docker) | PostgreSQL, Observability, Vaultwarden, SearXNG, Joplin, Portainer, Watchtower |
| N5 Pro | CT 201 (n5-docker) | PostgreSQL, Immich, Frigate, NextCloud, Portainer, Watchtower |

Service placement is configured via `ansible/inventory/host_vars/*/vars.yml` — the
`services` list variable controls which compose stacks deploy to which Docker host.

## Port Reference

### EQ12

| Port | Service |
|---|---|
| 3000 | Grafana |
| 5432 | PostgreSQL |
| 8086 | Vaultwarden |
| 8089 | VictoriaMetrics (InfluxDB) |
| 8428 | VictoriaMetrics (HTTP API) |
| 8686 | Vector (GraphQL API) |
| 9428 | VictoriaLogs (HTTP API) |
| 10080 | pgAdmin |
| 18080 | SearXNG |
| 22300 | Joplin |

### N5 Pro

| Port | Service |
|---|---|
| 2283 | Immich |
| 5000 | Frigate (Web UI) |
| 5432 | PostgreSQL |
| 8080 | NextCloud |
| 8554 | Frigate (RTSP) |
| 8555 | Frigate (WebRTC) |

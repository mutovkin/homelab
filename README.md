# Homelab

Multi-machine homelab configuration managed with **Ansible** and **Docker Compose**.

## Machines

| Machine                              | CPU                                 | RAM                           | Storage              | Role                                                              |
| ------------------------------------ | ----------------------------------- | ----------------------------- | -------------------- | ----------------------------------------------------------------- |
| [Beelink EQ12 Pro](docs/eq12.md)     | Intel N100, 4 cores                 | 16GB                          | 2TB NVMe (ZFS)       | Proxmox — Home Assistant, Docker services, Nginx Proxy Manager    |
| [Minisforum N5 Pro](docs/n5pro.md)   | AMD Ryzen AI 9 HX PRO 370, 12c/24t  | 96GB (32GB GPU / 62GB system) | 8TB NVMe + 130TB HDD | Proxmox — TrueNAS, Immich, Frigate, NextCloud                     |

## Architecture

Two-layer automation — see [docs/architecture.md](docs/architecture.md) for full details.

| Layer                         | Tool                     | What it manages                                                 |
| ----------------------------- | ------------------------ | --------------------------------------------------------------- |
| Host OS + VM/LXC provisioning | Ansible                  | Proxmox packages, repos, ZFS, GPU passthrough, VM/LXC lifecycle |
| Services                      | Ansible + Docker Compose | Docker install, `.env` templating, compose stack deployment     |

## Quick Start

### Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) (2.15+)
- [Task](https://taskfile.dev/installation/) (task runner)
- SSH access to both Proxmox hosts

### Setup

```bash
# Install Ansible Galaxy collections
cd ansible && ansible-galaxy install -r requirements.yml
```

### Deploy

```bash
# Full deployment (all steps in order)
task deploy:full

# Or step by step:
task infra:hosts      # 1. Configure Proxmox OS + provision VMs/LXCs
task infra:guests     # 2. Configure Docker inside VMs/LXCs
task deploy:services  # 3. Deploy compose stacks
```

## Repository Structure

```ascii
homelab/
├── docs/                  # Machine specs + architecture diagrams
│   ├── eq12.md
│   ├── n5pro.md
│   └── architecture.md
├── ansible/               # Ansible — host config, VM/LXC provisioning, service deployment
│   ├── inventory/         # Hosts, group vars, host vars, vault
│   ├── playbooks/         # Orchestration playbooks
│   └── roles/             # common, proxmox_host, proxmox_guests, docker_host, services/*
├── containers/            # Docker Compose stacks (standalone-usable)
│   ├── postgresql/
│   ├── observability/
│   ├── vaultwarden/
│   ├── searxng/
│   ├── joplin/
│   ├── immich/
│   ├── frigate/
│   ├── nextcloud/
│   ├── portainer/
│   └── watchtower/
├── REFACTORING.md         # Architectural decisions and proposal
└── TASKS.md               # Implementation tracking
```

## Container Services

### EQ12 (CT 101 — deb-docker)

| Service                                    | Port             | Description                              |
| ------------------------------------------ | ---------------- | ---------------------------------------- |
| [PostgreSQL](containers/postgresql/)       | 5432, 10080      | Database server + pgAdmin                |
| [Observability](containers/observability/) | 8428, 9428, 3000 | VictoriaMetrics + VictoriaLogs + Grafana |
| [Vaultwarden](containers/vaultwarden/)     | 8086             | Bitwarden-compatible password manager    |
| [SearXNG](containers/searxng/)             | 18080            | Privacy-respecting search engine         |
| [Joplin](containers/joplin/)               | 22300            | Note-taking server                       |
| [Portainer](containers/portainer/)         | 9000             | Container management UI                  |
| [Watchtower](containers/watchtower/)       | —                | Automatic container updates              |

### N5 Pro (CT 201 — n5pro-docker)

| Service                                 | Port             | Description                                          |
| --------------------------------------- | ---------------- | ---------------------------------------------------- |
| [PostgreSQL](containers/postgresql/)    | 5432             | Database for Immich + NextCloud                      |
| [Immich](containers/immich/)            | 2283             | Self-hosted photo/video management (GPU-accelerated) |
| [Frigate](containers/frigate/)          | 5000, 8554, 8555 | NVR with AI object detection (GPU-accelerated)       |
| [NextCloud](containers/nextcloud/)      | 8080             | File sync and collaboration                          |
| [Portainer](containers/portainer/)      | 9000             | Container management UI                              |
| [Watchtower](containers/watchtower/)    | —                | Automatic container updates                          |

## Documentation

- [REFACTORING.md](REFACTORING.md) — Architecture decisions, consolidation rationale
- [TASKS.md](TASKS.md) — Implementation progress tracking
- [docs/architecture.md](docs/architecture.md) — Network topology, orchestration flow, port map
- [docs/eq12.md](docs/eq12.md) — EQ12 hardware, VM/LXC inventory, ZFS layout
- [docs/n5pro.md](docs/n5pro.md) — N5 Pro hardware, GPU config, planned workloads

```shell
modinfo zfs | grep parm
```

### ZFS Pool Configuration Notes

- **Feature Set**: Pool supports upgradeable features (use `zpool upgrade rpool` to enable)
- **Scrub Schedule**: Automated scrubbing enabled for data integrity verification
- **Compression**: LZ4 compression enabled by default
- **Snapshots**: Available for all datasets

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

## Secrets Strategy

| Secret type                                  | Stored in                                | Encrypted by    |
| -------------------------------------------- | ---------------------------------------- | --------------- |
| Service credentials (DB passwords, API keys) | `ansible/inventory/**/vault.yml`         | ansible-vault   |
| Proxmox API tokens                           | `ansible/inventory/**/vault.yml`         | ansible-vault   |
| `.env` files on target hosts                 | Templated at deploy time from vault vars | Never committed |

## Networking

- **Cross-host:** Direct LAN (both machines on the same 192.168.x.x network)
- **Docker networks:** 172.x.x.x ranges (avoid LAN conflicts). EQ12: 172.20-25.x. N5 Pro: 172.30-35.x.
- **Centralized monitoring:** Telegraf on each machine → VictoriaMetrics on EQ12
- **NFS:** N5 Pro Docker LXC → TrueNAS VM for Frigate recordings, media storage, and Lyrion music library

## Hardware Passthrough

- **N5 Pro GPU** — AMD Radeon 890M with 32GB UMA allocation. Used via VAAPI `/dev/dri` device sharing (not full PCI passthrough) in CT-201 for Frigate and Immich.
- **TrueNAS SATA** — JMicron JMB58x controller at c1:00.0 uses full PCI passthrough in VM-200 (requires VM, not LXC).
- **TrueNAS NVMe** — 2× WD SN850X 2TB at c6:00.0 and c3:00.0 passed through to VM-200 for mirrored ZFS special vdev.

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
│   ├── lyrion/
│   ├── portainer/
│   └── watchtower/
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
| [Lyrion](containers/lyrion/)            | 9001, 9090, 3483 | Music server (Squeezebox/LMS) — NFS from TrueNAS     |
| [Portainer](containers/portainer/)      | 9000             | Container management UI                              |
| [Watchtower](containers/watchtower/)    | —                | Automatic container updates                          |

## Documentation

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

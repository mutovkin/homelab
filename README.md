# Homelab

Multi-machine homelab configuration managed with **Ansible**, **Pulumi**, and **Docker Compose**.

## Machines

| Machine | CPU | RAM | Storage | Role |
|---|---|---|---|---|
| [Beelink EQ12 Pro](docs/eq12.md) | Intel N100, 4 cores | 16GB | 2TB NVMe (ZFS) | Proxmox — Home Assistant, Docker services, Nginx Proxy Manager |
| [Minisforum N5 Pro](docs/n5pro.md) | AMD | 96GB (32GB GPU / 64GB system) | TBD | Proxmox 9.1.5 — TrueNAS, GPU workloads, Docker services |

## Architecture

Three-layer automation — see [docs/architecture.md](docs/architecture.md) for full details.

| Layer | Tool | What it manages |
|---|---|---|
| Host OS | Ansible | Proxmox packages, repos, ZFS, networking, GPU passthrough |
| VM/LXC lifecycle | Pulumi (TypeScript) | Create/update/delete VMs and LXCs with state tracking |
| Services | Ansible + Docker Compose | Docker install, `.env` templating, compose stack deployment |

## Quick Start

### Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) (2.15+)
- [Pulumi](https://www.pulumi.com/docs/install/) (3.x) + Node.js (20+)
- [Task](https://taskfile.dev/installation/) (task runner)
- SSH access to both Proxmox hosts

### Setup

```bash
# Install Ansible Galaxy collections
cd ansible && ansible-galaxy install -r requirements.yml

# Install Pulumi dependencies
cd infrastructure && npm install

# Configure Pulumi (one-time)
pulumi login --local
pulumi stack init prod
```

### Deploy

```bash
# Full deployment (all steps in order)
task deploy:full

# Or step by step:
task infra:hosts      # 1. Configure Proxmox OS on both machines
task infra:up         # 2. Create/update VMs and LXCs
task infra:guests     # 3. Configure Docker inside VMs/LXCs
task deploy:services  # 4. Deploy compose stacks
```

## Repository Structure

```ascii
homelab/
├── docs/                  # Machine specs + architecture diagrams
│   ├── eq12.md
│   ├── n5pro.md
│   └── architecture.md
├── infrastructure/        # Pulumi (TypeScript) — VM/LXC lifecycle
│   ├── machines/          # Per-machine VM/LXC definitions
│   └── components/        # Reusable LXC + VM component resources
├── ansible/               # Ansible — host config + service deployment
│   ├── inventory/         # Hosts, group vars, host vars, vault
│   ├── playbooks/         # Orchestration playbooks
│   └── roles/             # common, proxmox_host, docker_host, services/*
├── containers/            # Docker Compose stacks (standalone-usable)
│   ├── postgresql/
│   ├── observability/
│   ├── vaultwarden/
│   ├── searxng/
│   ├── joplin/
│   ├── portainer/
│   └── watchtower/
├── REFACTORING.md         # Architectural decisions and proposal
└── TASKS.md               # Implementation tracking
```

## Container Services

| Service | Port | Description |
|---|---|---|
| [PostgreSQL](containers/postgresql/) | 5432, 10080 | Database server + pgAdmin |
| [Observability](containers/observability/) | 8428, 9428, 3000 | VictoriaMetrics + VictoriaLogs + Grafana |
| [Vaultwarden](containers/vaultwarden/) | 8086 | Bitwarden-compatible password manager |
| [SearXNG](containers/searxng/) | — | Privacy-respecting search engine |
| [Joplin](containers/joplin/) | — | Note-taking server |
| [Portainer](containers/portainer/) | — | Container management UI |
| [Watchtower](containers/watchtower/) | — | Automatic container updates |

## Documentation

- [REFACTORING.md](REFACTORING.md) — Architecture decisions, three-layer rationale, secrets strategy
- [TASKS.md](TASKS.md) — Implementation progress tracking
- [docs/architecture.md](docs/architecture.md) — Network topology, orchestration flow, port map
- [docs/eq12.md](docs/eq12.md) — EQ12 hardware, VM/LXC inventory, ZFS layout
- [docs/n5pro.md](docs/n5pro.md) — N5 Pro hardware, GPU config, planned workloads

# zpool upgrade rpool

# Manually start a scrub (data integrity check)

zpool scrub rpool

# Check scrub status

zpool status -v rpool

# Show ZFS ARC cache statistics

cat /proc/spl/kstat/zfs/arcstats | grep -E "^(size|hits|misses|c_max)"

# Show ZFS module parameters

modinfo zfs | grep parm

```

#### ZFS Pool Configuration Notes

- **Feature Set**: Pool supports upgradeable features (use `zpool upgrade rpool` to enable)
- **Scrub Schedule**: Automated scrubbing enabled for data integrity verification
- **Compression**: LZ4 compression enabled by default
- **Snapshots**: Available for all datasets

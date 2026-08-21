# Homelab

[![CI](https://github.com/mutovkin/homelab/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/mutovkin/homelab/actions/workflows/ci.yml)

Multi-machine homelab configuration managed with **Ansible** and **Docker Compose**.

## Machines

| Machine                              | CPU                                 | RAM                           | Storage              | Role                                                              |
| ------------------------------------ | ----------------------------------- | ----------------------------- | -------------------- | ----------------------------------------------------------------- |
| [Beelink EQ12 Pro](docs/eq12.md)     | Intel N100, 4 cores                 | 16GB                          | 2TB NVMe (ZFS)       | Proxmox — Home Assistant, Docker services, Nginx Proxy Manager    |
| [Minisforum N5 Pro](docs/n5pro.md)   | AMD Ryzen AI 9 HX PRO 370, 12c/24t  | 96GB (32GB GPU / 62GB system) | 8TB NVMe + 130TB HDD | Proxmox — TrueNAS, Docker services (LMS), Portainer               |

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
- **Docker networks:** 172.x.x.x ranges (avoid LAN conflicts). EQ12: 172.20–25.x pins (pool 172.20.0.0/14). N5 Pro: 172.26–29.x + .31 pins (pool 172.18.0.0/15 — fleet map in `host_vars/n5pro_docker`).
- **Centralized monitoring:** Telegraf on each machine → VictoriaMetrics on EQ12
- **NFS:** N5 Pro Docker LXC → TrueNAS VM for the Lyrion music library today; Frigate recordings and other media once those stacks land (#91)

## Hardware Passthrough

- **N5 Pro GPU** — AMD Radeon 890M with 32GB UMA allocation. Reserved for Frigate and Immich (both planned #91) via VAAPI `/dev/dri` device sharing (not full PCI passthrough) in CT-201 — no deployed workload uses it today.
- **TrueNAS SATA** — JMicron JMB58x controller at c1:00.0 uses full PCI passthrough in VM-200 (requires VM, not LXC).
- **TrueNAS NVMe** — 2× WD SN850X 2TB at c6:00.0 and c3:00.0 passed through to VM-200 for mirrored ZFS special vdev.

## Repository Structure

```ascii
homelab/
├── docs/                  # Machine specs + architecture diagrams
│   ├── eq12.md
│   ├── n5pro.md
│   ├── ups.md
│   ├── architecture.md
│   └── solutions/         # Documented fixes to past problems, by category
└── ansible/               # Ansible — host config, VM/LXC provisioning, service deployment
    ├── inventory/         # Hosts, group vars, host vars, vault
    ├── playbooks/         # Orchestration playbooks
    └── roles/             # common, proxmox_host, proxmox_guests, docker_host, nut,
                           # services/* (compose.yaml + env.j2 per service)
```

Each service is one self-contained role: `ansible/roles/services/<svc>/` holds its
`files/compose.yaml`, any shipped configs under `files/`, its `templates/env.j2`, and
its README. The shared `services/_deploy` role runs the deploy pipeline for all of them.

## Container Services

### EQ12 (CT 101 — deb-docker)

| Service                                    | Port             | Description                              |
| ------------------------------------------ | ---------------- | ---------------------------------------- |
| [PostgreSQL](ansible/roles/services/postgresql/)       | 5432, 10080      | Database server + pgAdmin                |
| [Observability](ansible/roles/services/observability/) | 8428, 9428, 3000 | VictoriaMetrics + VictoriaLogs + Grafana |
| [Vaultwarden](ansible/roles/services/vaultwarden/)     | 8086             | Bitwarden-compatible password manager    |
| [SearXNG](ansible/roles/services/searxng/)             | 18080            | Privacy-respecting search engine         |
| [Joplin](ansible/roles/services/joplin/)               | 22300            | Note-taking server                       |
| [Portainer](ansible/roles/services/portainer/)         | 9000 (allowlisted) | Container management UI                |
| [Watchtower](ansible/roles/services/watchtower/)       | —                | Scheduled container updates (per-service policy: auto vs monitor-only) |

### N5 Pro (CT 201 — n5pro-docker)

| Service                                 | Port             | Description                                          |
| --------------------------------------- | ---------------- | ---------------------------------------------------- |
| [LMS](ansible/roles/services/lms/)               | 9001, 9090, 3483 | Music server (Lyrion/Squeezebox) — NFS from TrueNAS   |
| [PostgreSQL](ansible/roles/services/postgresql/) | 5432 (planned)   | _(planned #91)_ Database for Immich + NextCloud      |
| [Immich](ansible/roles/services/immich/)         | 2283 (planned)   | _(planned #91)_ Self-hosted photo/video management (GPU-accelerated) |
| [Frigate](ansible/roles/services/frigate/)       | 5000, 8554, 8555 (planned) | _(planned #91)_ NVR with AI object detection (GPU-accelerated) |
| [NextCloud](ansible/roles/services/nextcloud/)   | 8080 (planned)   | _(planned #91)_ File sync and collaboration          |
| [Portainer](ansible/roles/services/portainer/)   | 9000 (allowlisted) | Container management UI                            |
| [Watchtower](ansible/roles/services/watchtower/) | —                | Scheduled container updates (per-service policy: auto vs monitor-only) |

### Host-level services (not compose)

| Role                                             | Hosts                          | Description                                          |
| ------------------------------------------------ | ------------------------------ | ---------------------------------------------------- |
| [vector_agent](ansible/roles/vector_agent/)      | eq12, n5pro, n5pro_docker      | Native systemd Vector log shipper → VictoriaLogs on eq12_docker (#134). No listening port. |
| [rsyslog_structured](ansible/roles/rsyslog_structured/) | all four                | The RFC5424 `/var/log/structured.log` side-stream every Vector reads |

`task deploy:logagents` deploys the agents; `site.yml` runs them last, after the
compose stacks, because their final assertion queries VictoriaLogs.

## Documentation

- [docs/architecture.md](docs/architecture.md) — Network topology, orchestration flow, port map
- [docs/eq12.md](docs/eq12.md) — EQ12 hardware, VM/LXC inventory, ZFS layout
- [docs/n5pro.md](docs/n5pro.md) — N5 Pro hardware, GPU config, planned workloads
- [docs/ups.md](docs/ups.md) — UPS / NUT power management
- [docs/solutions/](docs/solutions/) — documented fixes to past problems, by category

### ZFS Pool Configuration Notes

- **Feature Set**: Pool supports upgradeable features (use `zpool upgrade rpool` to enable)
- **Scrub Schedule**: Automated scrubbing enabled for data integrity verification
- **Compression**: LZ4 compression enabled by default
- **Snapshots**: Available for all datasets

Inspect the pool's tunable module parameters with:

```shell
modinfo zfs | grep parm
```

## License

[MIT](LICENSE) © Serguei Moutovkin

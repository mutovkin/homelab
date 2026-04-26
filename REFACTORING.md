# Homelab Repository Refactoring

## Motivation

This repository started as configuration for a single Beelink EQ12 Pro machine. With the addition of a Minisforum N5 Pro (96GB RAM, AMD Radeon 890M GPU, Proxmox 9.1.6), the repo evolved into a multi-machine homelab monorepo with proper automation.

### Original Pain Points

- **Single-machine assumption** — Directory structure, networking, and docs all assumed one Proxmox host
- **No automation** — VMs/LXCs created manually via Proxmox UI, Docker stacks deployed with manual `docker compose up`
- **No state tracking** — If a VM or LXC is deleted, nothing detects the drift or can recreate it
- **Manual secrets** — `.env` files created by hand from `.env.example`, not version-controlled
- **Not reproducible** — A failed disk means manually rebuilding everything from memory + docs

## Machines

| Machine | CPU | RAM | Storage | Role |
|---|---|---|---|---|
| Beelink EQ12 Pro | Intel N100, 4 cores | 16GB | 2TB NVMe (ZFS) | Proxmox — HA, Docker services, Nginx Proxy Manager |
| Minisforum N5 Pro | AMD Ryzen AI 9 HX PRO 370, 12c/24t | 96GB (32GB GPU UMA) | 8TB NVMe + 5×26TB HDD | Proxmox — TrueNAS, Immich, Frigate, NextCloud |

## Architecture: Two-Layer Automation

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 2: Docker Compose                                     │
│  Service definitions in containers/                          │
│  Deployed by Ansible service roles                           │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Ansible                                            │
│  Proxmox host config + VM/LXC provisioning + guest config    │
│  + service deployment — single `site.yml` runs everything    │
└──────────────────────────────────────────────────────────────┘
```

### Orchestration Flow

```bash
# Single command, end-to-end:
task deploy:full
# Equivalent to: ansible-playbook ansible/playbooks/site.yml

# Step by step:
task infra:hosts      # 1. Configure Proxmox OS + provision VMs/LXCs
task infra:guests     # 2. Configure guests (Docker, packages)
task deploy:services  # 3. Deploy compose stacks
```

## Directory Structure

```
homelab/
├── README.md
├── REFACTORING.md                         # This document
├── TASKS.md                               # Implementation tracking
├── Taskfile.yml                           # Orchestration wrapper
│
├── docs/
│   ├── architecture.md                    # Topology, networks, ports, GPU passthrough
│   ├── eq12.md                            # EQ12 hardware specs
│   └── n5pro.md                           # N5 Pro hardware, GPU config
│
├── ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── inventory/
│   │   ├── hosts.yml
│   │   ├── host_vars/{eq12,n5pro}/        # VM/LXC definitions + per-host vault
│   │   └── group_vars/all/                # Shared config + secrets
│   ├── playbooks/
│   │   ├── site.yml                       # Master — runs everything end-to-end
│   │   ├── proxmox-hosts.yml              # Host config + VM/LXC provisioning
│   │   ├── configure-guests.yml           # Docker installation
│   │   └── deploy-services.yml            # Compose stack deployment
│   └── roles/
│       ├── common/
│       ├── proxmox_host/
│       ├── proxmox_guests/                # VM/LXC provisioning via Proxmox API
│       ├── docker_host/
│       └── services/{postgresql,observability,immich,frigate,...}/
│
└── containers/                            # Docker Compose (standalone-usable)
    └── {postgresql,observability,immich,frigate,nextcloud,...}/
```

## Secrets Strategy

| Secret type | Stored in | Encrypted by |
|---|---|---|
| Service credentials (DB passwords, API keys) | `ansible/inventory/**/vault.yml` | ansible-vault |
| Proxmox API tokens | `ansible/inventory/**/vault.yml` | ansible-vault |
| `.env` files on target hosts | Templated at deploy time from vault vars | Never committed |

## Networking

- **Cross-host:** Direct LAN (both machines on the same 192.168.x.x network)
- **Docker networks:** 172.x.x.x ranges (avoid LAN conflicts). EQ12: 172.20-25.x. N5 Pro: 172.30-35.x.
- **Centralized monitoring:** Telegraf on each machine → VictoriaMetrics on EQ12
- **NFS:** N5 Pro Docker LXC → TrueNAS VM for Frigate recordings and media storage

## Resolved Questions

1. **N5 Pro GPU** — AMD Radeon 890M with 32GB UMA allocation. Used via VAAPI `/dev/dri` device sharing (not full PCI passthrough) in CT-201 for Frigate and Immich.
2. **TrueNAS SATA passthrough** — JMicron JMB58x controller at c1:00.0 uses full PCI passthrough in VM-200 (requires VM, not LXC).

# Homelab Repository Refactoring Proposal

## Motivation

This repository started as configuration for a single Beelink EQ12 Pro machine. With the addition of a Minisforum N5 Pro (96GB RAM, Proxmox 9.1.5), the repo needs to evolve into a multi-machine homelab monorepo with proper automation.

### Current Pain Points

- **Single-machine assumption** — Directory structure, networking, and docs all assume one Proxmox host
- **No automation** — VMs/LXCs created manually via Proxmox UI, Docker stacks deployed with manual `docker compose up`
- **No state tracking** — If a VM or LXC is deleted, nothing detects the drift or can recreate it
- **Manual secrets** — `.env` files created by hand from `.env.example`, not version-controlled
- **Not reproducible** — A failed disk means manually rebuilding everything from memory + docs

## Machines

| Machine | CPU | RAM | Storage | Role |
|---|---|---|---|---|
| Beelink EQ12 Pro | Intel N100, 4 cores | 16GB | 2TB NVMe (ZFS) | Proxmox host — HA, Docker services, Nginx Proxy Manager |
| Minisforum N5 Pro | AMD (TBD) | 96GB (32GB GPU / 64GB system) | TBD | Proxmox 9.1.5 — TrueNAS, GPU workloads, Docker services |

## Architecture: Three-Layer Automation

### Why Three Tools?

Each layer of the homelab has different requirements. No single tool is optimal for all three:

```
┌─────────────────────────────────────────────────┐
│  Layer 3: Docker Compose (service definitions)  │
│  Deployed by Ansible service roles              │
├─────────────────────────────────────────────────┤
│  Layer 2: Pulumi/TypeScript (VM/LXC lifecycle)  │
│  Stateful — tracks what exists, diffs, deletes  │
├─────────────────────────────────────────────────┤
│  Layer 1: Ansible (Proxmox host OS config)      │
│  Convergent — packages, ZFS, networking, GPU    │
└─────────────────────────────────────────────────┘
```

### Layer 1: Ansible — Proxmox Host Configuration

**What it manages:** Proxmox OS packages, repositories, ZFS pool/scrub schedules, network bridges, GPU passthrough (IOMMU, vfio-pci), kernel parameters, SSH keys.

**Why Ansible:** Host configuration is convergent — you declare "these packages should be installed, this config file should have these contents" and Ansible makes it so. There's no meaningful "state" to track; re-running the same playbook is always safe and idempotent.

### Layer 2: Pulumi (TypeScript) — VM/LXC Lifecycle

**What it manages:** Creation, modification, and deletion of VMs and LXC containers on both Proxmox hosts. Includes resource allocation (cores, memory, disk), network interfaces, PCI passthrough assignments, and cloud-init configuration.

**Why Pulumi over Ansible for this layer:**

- **State tracking** — Pulumi maintains a state file of what resources exist. If you remove an LXC from the code and run `pulumi up`, it deletes the LXC from Proxmox. Ansible has no equivalent — it runs tasks but doesn't know what *should not* exist.
- **Drift detection** — `pulumi preview` shows what changed between your code and reality. Ansible's `--check` mode only shows what its tasks *would do*, not whether someone manually changed a VM's memory in the Proxmox UI.
- **CDK-like experience** — TypeScript with typed resource definitions feels like AWS CDK. Reusable `ComponentResource` classes for LXCs and VMs with autocompletion and compile-time checks.
- **Diff before apply** — `pulumi preview` shows a clear diff of what will be created/updated/deleted before any changes happen.

**Why Pulumi over Terraform:**

- TypeScript instead of HCL — more expressive, real programming language, familiar to CDK users
- `@muhlba91/pulumi-proxmoxve` is a native Pulumi provider (not a Terraform bridge), actively maintained
- Pulumi state can be stored locally (`pulumi login --local`) with no cloud dependency

**Why NOT Pulumi for host config:**

- Pulumi is designed for resource lifecycle, not configuration management. Installing apt packages or editing config files inside a running machine is Ansible's domain.

### Layer 3: Ansible + Docker Compose — Service Deployment

**What it manages:** Installing Docker inside VMs/LXCs, templating `.env` files from vault-encrypted secrets, syncing compose files + config, running `docker compose up`.

**Why Ansible (not Pulumi) for this layer:**

- Compose files are the natural service definition format — well-understood, portable, debuggable with plain `docker compose up`
- Ansible's `community.docker.docker_compose_v2` module wraps compose natively
- Ansible-vault provides secret injection into `.env` templates without a separate secrets manager
- The `containers/` directory stays standalone-usable for manual debugging

### Orchestration Flow

```
1. ansible-playbook proxmox-hosts.yml      # Configure Proxmox OS on both machines
2. cd infrastructure && pulumi up           # Create/update/delete VMs & LXCs
3. ansible-playbook configure-guests.yml    # Install Docker inside VMs/LXCs
4. ansible-playbook deploy-services.yml     # Deploy compose stacks
```

A `Taskfile.yml` at repo root wraps these as: `task infra:up`, `task config:hosts`, `task deploy:all`.

## Directory Structure

```
homelab/
├── README.md                              # Overview, quickstart
├── REFACTORING.md                         # This document
├── TASKS.md                               # Implementation tracking
├── Taskfile.yml                           # Orchestration wrapper
│
├── docs/
│   ├── architecture.md                    # Network topology, cross-host dependencies
│   ├── eq12.md                            # EQ12 hardware, current VM/LXC inventory
│   └── n5pro.md                           # N5 Pro hardware, GPU config, TrueNAS plans
│
├── infrastructure/                        # Pulumi (TypeScript)
│   ├── package.json
│   ├── tsconfig.json
│   ├── Pulumi.yaml / Pulumi.prod.yaml
│   ├── index.ts
│   ├── machines/
│   │   ├── eq12.ts                        # EQ12: HA VM-100, deb-docker CT-101, etc.
│   │   └── n5pro.ts                       # N5 Pro: TrueNAS VM, Docker LXC
│   └── components/
│       ├── lxc-container.ts               # Reusable ComponentResource
│       └── virtual-machine.ts             # Reusable ComponentResource
│
├── ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── inventory/
│   │   ├── hosts.yml                      # proxmox_hosts + docker_hosts
│   │   ├── host_vars/{eq12,n5pro}/        # Per-machine vars + vault
│   │   └── group_vars/all/                # Shared config + secrets
│   ├── playbooks/
│   │   ├── site.yml
│   │   ├── proxmox-hosts.yml
│   │   ├── configure-guests.yml
│   │   └── deploy-services.yml
│   └── roles/
│       ├── common/
│       ├── proxmox_host/
│       ├── docker_host/
│       └── services/{postgresql,observability,...}/
│
└── containers/                            # Docker Compose (preserved, standalone-usable)
    └── {postgresql,observability,vaultwarden,...}/
```

## Secrets Strategy

| Secret type | Stored in | Encrypted by |
|---|---|---|
| Service credentials (DB passwords, API keys) | `ansible/inventory/**/vault.yml` | ansible-vault |
| Proxmox API tokens (for Pulumi) | `Pulumi.prod.yaml` | `pulumi config set --secret` |
| `.env` files on target hosts | Templated at deploy time from vault vars | Never committed |

**Bitwarden circular dependency resolved:** Vault-encrypted secrets in the repo can bootstrap Vaultwarden itself. Bitwarden remains the human-facing password manager; ansible-vault is the machine-facing secret store.

## Networking

- **Cross-host:** Direct LAN (simplest for two machines on the same network)
- **Docker networks:** 172.x.x.x ranges preserved (avoid 192.168.x.x LAN conflicts)
- **Centralized monitoring:** Telegraf on each machine → VictoriaMetrics on whichever host runs the observability stack

## Scope

**Included:**

- Repository restructuring + documentation
- Ansible roles for Proxmox host config and Docker service deployment
- Pulumi project for VM/LXC lifecycle on both machines
- Secrets migration to ansible-vault + pulumi config
- Taskfile for orchestration

**Excluded:**

- TrueNAS internal configuration (managed via TrueNAS UI/API)
- GPU workload container definitions (deferred until N5 Pro services are decided)
- Network security hardening (later phase)
- Service migration between machines (deferred — service placement decided later)

## Open Questions

1. **N5 Pro GPU** — Is the 32GB GPU allocation for AMD integrated graphics? Which VMs need GPU access?
2. **Pulumi import** — Existing EQ12 VMs/LXCs (100, 101, 102, 103) must be imported into Pulumi state before management. One-time operation, critical to avoid recreation.
3. **Ansible invokes Pulumi?** — Orchestration requires Pulumi between two Ansible steps. Taskfile handles this, but `site.yml` alone can't do a full rebuild. Alternative: Ansible calls Pulumi via shell task.

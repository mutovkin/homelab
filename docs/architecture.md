# Architecture

## Machine Topology

```ascii
                         LAN (192.168.x.x)
                    ┌───────────┴───────────┐
                    │                       │
            ┌───────┴───────┐       ┌───────┴───────┐
            │  Beelink EQ12 │       │ Minisforum N5 │
            │  Proxmox (pve)│       │  Proxmox 9.1.5│
            │  16GB / N100  │       │ 96GB / AMD    │
            └───────┬───────┘       └───────┬───────┘
                    │                       │
        ┌───────────┼───────────┐       ┌───┼───────────┐
        │           │           │       │   │           │
   ┌────┴────┐ ┌────┴─────┐ ┌───┴───┐  ┌┴───┴──┐  ┌─────┴───┐
   │ VM-100  │ │ CT-101   │ │CT-103 │  │TrueNAS│  │Docker   │
   │ Home    │ │deb-docker│ │ NPM   │  │  VM   │  │  LXC    │
   │Assistant│ │(Docker)  │ │       │  │       │  │(planned)│
   └─────────┘ └────┬─────┘ └───────┘  └───────┘  └─────────┘
                    │
    ┌───────┬───────┼───────┬──────┬──────┬──────┐
    │       │       │       │      │      │      │
   PG    Obs    Vault    SearX  Joplin  Port  Watch
```

## Automation Layers

```ascii
┌──────────────────────────────────────────────────────────────┐
│  Layer 3: Docker Compose                                     │
│  Service definitions in containers/                          │
│  Deployed by Ansible service roles                           │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: Pulumi (TypeScript)                                │
│  VM/LXC lifecycle — stateful, drift detection, diff preview  │
│  Defined in infrastructure/                                  │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Ansible                                            │
│  Proxmox host OS config — convergent, idempotent             │
│  Defined in ansible/                                         │
└──────────────────────────────────────────────────────────────┘
```

### Orchestration

```bash
# Full rebuild sequence:
ansible-playbook ansible/playbooks/proxmox-hosts.yml   # 1. Configure Proxmox OS
cd infrastructure && pulumi up                          # 2. Create VMs/LXCs
ansible-playbook ansible/playbooks/configure-guests.yml # 3. Configure guests (Docker, etc.)
ansible-playbook ansible/playbooks/deploy-services.yml  # 4. Deploy compose stacks

# Or via Taskfile:
task infra:hosts      # Step 1
task infra:up         # Step 2
task infra:guests     # Step 3
task deploy:all       # Step 4
```

## Docker Network Map (EQ12 — CT 101)

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

## Cross-Host Communication

- **Method**: Direct LAN (both machines on the same 192.168.x.x network)
- **Use cases**:
  - Centralized monitoring: Telegraf on N5 Pro → VictoriaMetrics on EQ12 (or vice versa)
  - Shared database: Services on N5 Pro → PostgreSQL on EQ12 (if needed)
- **Configuration**: LAN IPs templated into `.env` files by Ansible using inventory variables

## Service Placement

Currently all services run on EQ12 (CT 101 — deb-docker). Service placement across machines will be decided later and is configured via `ansible/inventory/host_vars/*/vars.yml` — the `services` list variable controls which compose stacks deploy to which Docker host.

## Port Reference

| Port | Service | Host |
|---|---|---|
| 3000 | Grafana | EQ12 |
| 5432 | PostgreSQL | EQ12 |
| 8086 | Vaultwarden | EQ12 |
| 8089 | VictoriaMetrics (InfluxDB) | EQ12 |
| 8428 | VictoriaMetrics (HTTP API) | EQ12 |
| 8686 | Vector (GraphQL API) | EQ12 |
| 9428 | VictoriaLogs (HTTP API) | EQ12 |
| 10080 | pgAdmin | EQ12 |

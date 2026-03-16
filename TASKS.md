# Implementation Tasks

Tracking implementation progress for the homelab repository refactoring.
See [REFACTORING.md](REFACTORING.md) for the full proposal and architectural decisions.

## Status Legend

- ⬜ Not started
- 🔄 In progress
- ✅ Complete

---

## Phase 1: Repository Scaffolding

| Status | Task | Notes |
|---|---|---|
| ✅ | Create `docs/` directory with `eq12.md`, `n5pro.md`, `architecture.md` | Extracted EQ12 specs; created N5 Pro docs; architecture with topology diagram |
| ✅ | Rewrite root `README.md` for multi-machine overview | Quick start, repo structure, service table |
| ✅ | Create `Taskfile.yml` with orchestration commands | deploy:full, infra:up, infra:hosts, etc. |
| ✅ | Update `.gitignore` for Pulumi + Ansible artifacts | Added node_modules, .vault_password, dist/ |

## Phase 2: Pulumi — VM/LXC Lifecycle

| Status | Task | Notes |
|---|---|---|
| ✅ | Initialize `infrastructure/` Pulumi TypeScript project | package.json, tsconfig, Pulumi.yaml, Pulumi.prod.yaml |
| ✅ | Create `components/lxc-container.ts` ComponentResource | Typed: cores, memory, disk, mountPoints, nesting, network |
| ✅ | Create `components/virtual-machine.ts` ComponentResource | Typed: cores, memory, disk, PCI passthrough, BIOS, ISO |
| ✅ | Define EQ12 infra in `machines/eq12.ts` | HA VM-100, deb-docker CT-101, ubuntu-docker CT-102, NPM CT-103 |
| ✅ | Define N5 Pro infra in `machines/n5pro.ts` | TrueNAS VM-200, Docker LXC CT-201, GPU VM placeholder |
| ⬜ | `pulumi import` existing EQ12 resources | Critical: adopt without recreating. Run after npm install. |

## Phase 3: Ansible — Host Configuration

| Status | Task | Notes |
|---|---|---|
| ✅ | Create `ansible/ansible.cfg` + `requirements.yml` | community.general, community.docker, ansible.posix |
| ✅ | Create inventory: `hosts.yml`, `host_vars/`, `group_vars/` | proxmox_hosts + docker_hosts; vault.yml scaffolds |
| ✅ | `roles/common` — base packages, timezone, SSH keys | defaults + tasks |
| ✅ | `roles/proxmox_host` — repos, ZFS, networking, GPU passthrough | IOMMU, vfio-pci, handlers for grub/initramfs |
| ✅ | `roles/docker_host` — Docker CE + compose + daemon.json | Jinja2 template for daemon.json |

## Phase 4: Ansible — Service Deployment

| Status | Task | Notes |
|---|---|---|
| ✅ | Create service role pattern (template for all services) | sync from containers/, template .env, docker_compose_v2 |
| ✅ | `roles/services/postgresql` | tasks + env.j2 |
| ✅ | `roles/services/observability` | tasks + env.j2 |
| ✅ | `roles/services/vaultwarden` | tasks + env.j2 |
| ✅ | `roles/services/searxng` | tasks + env.j2 |
| ✅ | `roles/services/joplin` | tasks + env.j2 |
| ✅ | `roles/services/portainer` | tasks (no .env needed) |
| ✅ | `roles/services/watchtower` | tasks + env.j2 |
| ⬜ | Cross-host service configuration (monitoring endpoints) | Deferred until services are placed on N5 Pro |

## Phase 5: Secrets Migration

| Status | Task | Notes |
|---|---|---|
| ✅ | Set up ansible-vault, add `.vault_password` to `.gitignore` | .vault_password in root .gitignore |
| ✅ | Create vault.yml scaffolds with expected variables | Per-host (eq12, n5pro) + shared (group_vars/all) |
| ✅ | Convert `.env.example` → Jinja2 `env.j2` templates | All 6 services with .env now have env.j2 templates |
| ⬜ | Encrypt actual secrets into `vault.yml` files | Requires real credentials — run ansible-vault create |
| ⬜ | Configure Pulumi secrets (`pulumi config set --secret`) | Requires Proxmox API tokens |

## Phase 6: Playbook Assembly

| Status | Task | Notes |
|---|---|---|
| ✅ | `playbooks/proxmox-hosts.yml` | common + proxmox_host roles |
| ✅ | `playbooks/configure-guests.yml` | common + docker_host roles |
| ✅ | `playbooks/deploy-services.yml` | Service roles with when conditions + tags |
| ✅ | `playbooks/site.yml` | Master playbook importing all three |

## Verification

| Status | Task |
|---|---|
| ⬜ | `pulumi preview` shows no changes after importing EQ12 |
| ⬜ | `ansible-lint` passes on all roles/playbooks |
| ⬜ | `ansible-playbook --check --diff` dry run on EQ12 — no changes |
| ⬜ | Single service deploy test (watchtower) |
| ⬜ | Full rebuild test on N5 Pro |
| ⬜ | Idempotency: second run shows zero changes |

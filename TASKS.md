# Implementation Tasks

Tracking implementation progress for the homelab repository.
See [REFACTORING.md](REFACTORING.md) for architectural decisions.

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
| ✅ | Create `Taskfile.yml` with orchestration commands | deploy:full, infra:hosts, etc. |
| ✅ | Update `.gitignore` for Ansible artifacts | Added .vault_password |

## Phase 2: Ansible — Host Configuration + VM/LXC Provisioning

| Status | Task | Notes |
|---|---|---|
| ✅ | Create `ansible/ansible.cfg` + `requirements.yml` | community.general, community.docker, ansible.posix |
| ✅ | Create inventory: `hosts.yml`, `host_vars/`, `group_vars/` | proxmox_hosts + docker_hosts; vault.yml scaffolds |
| ✅ | `roles/common` — base packages, timezone, SSH keys | defaults + tasks |
| ✅ | `roles/proxmox_host` — repos, ZFS, networking, GPU passthrough | IOMMU, vfio-pci, handlers for grub/initramfs |
| ✅ | `roles/proxmox_guests` — VM/LXC provisioning via Proxmox API | Replaces Pulumi; proxmox_kvm for VMs, proxmox for LXCs, GPU device passthrough |
| ✅ | `roles/docker_host` — Docker CE + compose + daemon.json | Jinja2 template for daemon.json |
| ✅ | Port VM/LXC definitions to host_vars | EQ12: VM-100, CT-101/102/104; N5 Pro: VM-200, CT-201 |

## Phase 3: Ansible — Service Deployment

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
| ✅ | `roles/services/immich` | tasks + env.j2; GPU device passthrough for ML |
| ✅ | `roles/services/frigate` | tasks + env.j2; VAAPI GPU, NFS storage from TrueNAS |
| ✅ | `roles/services/nextcloud` | tasks + env.j2; Redis cache, shared PG |

## Phase 4: Secrets Migration

| Status | Task | Notes |
|---|---|---|
| ✅ | Set up ansible-vault, add `.vault_password` to `.gitignore` | .vault_password in root .gitignore |
| ✅ | Create vault.yml scaffolds with expected variables | Per-host (eq12, n5pro) + shared (group_vars/all) |
| ✅ | Convert `.env.example` → Jinja2 `env.j2` templates | All services with .env now have env.j2 templates |
| ⬜ | Encrypt actual secrets into `vault.yml` files | Requires real credentials — run ansible-vault create |
| ⬜ | Add Proxmox API tokens to vault | Needed for proxmox_guests role |

## Phase 5: Playbook Assembly

| Status | Task | Notes |
|---|---|---|
| ✅ | `playbooks/proxmox-hosts.yml` | common + proxmox_host + proxmox_guests roles |
| ✅ | `playbooks/configure-guests.yml` | common + docker_host roles |
| ✅ | `playbooks/deploy-services.yml` | Service roles with when conditions + tags |
| ✅ | `playbooks/site.yml` | Master playbook — full end-to-end, no manual gap |
| ✅ | Update `Taskfile.yml` | Removed Pulumi tasks, simplified deploy:full |

## Phase 6: Documentation

| Status | Task | Notes |
|---|---|---|
| ✅ | Update README.md | Two-layer architecture, N5 Pro services, removed Pulumi |
| ✅ | Update ONBOARDING.md | Removed Pulumi setup/import steps |
| ✅ | Update docs/architecture.md | N5 Pro topology, network map, GPU passthrough, port reference |
| ✅ | Update REFACTORING.md | Document consolidation from Pulumi to Ansible-only |

## Verification

| Status | Task |
|---|---|
| ⬜ | `ansible-lint` passes on all roles/playbooks |
| ⬜ | `ansible-playbook --check --diff` dry run on EQ12 — no unexpected changes |
| ⬜ | Single service deploy test (watchtower on EQ12) |
| ⬜ | Provision N5 Pro Docker LXC (CT-201) via Ansible |
| ⬜ | Deploy watchtower/portainer to N5 Pro |
| ⬜ | Full `site.yml` end-to-end test |
| ⬜ | Idempotency: second run shows zero changes |

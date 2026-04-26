# Architecture Decisions

Technical decisions, tradeoffs, and rationale for the homelab container hosting strategy.

---

## 1. How to Run Docker in Proxmox

There are four common approaches. We use **Option B**.

### Option A: Docker directly on Proxmox host

Install Docker Engine on the Proxmox host itself (bare-metal).

| Pros | Cons |
|---|---|
| Zero overhead — no virtualization layer | Pollutes the hypervisor with container runtime |
| Simplest possible setup | `apt upgrade` on Proxmox can break Docker or vice versa |
| All RAM available to containers | No isolation — a runaway container can destabilize Proxmox |
| | Proxmox updates may overwrite kernel modules Docker depends on |
| | Can't snapshot/backup the Docker environment independently |

**Verdict:** Tempting for resource-constrained hosts but fragile. One bad Docker upgrade and you lose hypervisor access.

### Option B: Docker inside a privileged LXC (our approach)

Create a Proxmox LXC container, enable nesting, install Docker inside it.

| Pros | Cons |
|---|---|
| Near-zero overhead (~1-2% vs bare-metal) | Requires privileged or nesting-enabled LXC |
| LXC is snapshotable and backable-up independently | Some Docker features need extra LXC config (e.g., `/dev/dri` passthrough) |
| Clean separation: Proxmox host stays pristine | NFS mounts need `mp` entries in LXC config, not just `fstab` |
| Can set hard memory/CPU limits at the LXC level | AppArmor profiles can conflict with Docker |
| Docker Compose works normally inside | |
| Can share devices (`/dev/dri`) for GPU acceleration | |

**Our config** (CT-101 on EQ12, CT-201 on N5 Pro):
- Debian 12/13 minimal template
- `nesting: true` (enables Docker's overlayfs)
- `keyctl: true` (needed for some container runtimes)
- Resource capped: 2c/4GB (EQ12) and 8c/24GB (N5 Pro)
- Mount points for persistent data (`mp0` → `/data`)

**Why not unprivileged LXC?** Docker inside unprivileged LXC requires `lxc.idmap` remapping, which breaks bind mounts for most services (Immich, Frigate, PostgreSQL all expect UID 0 or specific UIDs). The security benefit is marginal on a homelab with no untrusted tenants.

### Option C: Docker inside a full VM

Create a QEMU VM, install a full Linux distro, then Docker.

| Pros | Cons |
|---|---|
| Complete isolation — own kernel | ~500MB–1GB RAM overhead per VM (kernel + systemd + drivers) |
| Any distro, any kernel version | Slower I/O than LXC (virtio still adds a layer) |
| Easy to migrate between Proxmox hosts | More disk space (full OS image) |
| GPU PCI passthrough works cleanly | Can't share GPU between VM and host |

**When to use:** Only when you need a different kernel version (e.g., specific GPU driver), full PCI passthrough (our TrueNAS VM-200), or compatibility with software that refuses to run in LXC.

### Option D: Proxmox-native LXC per service (no Docker)

Skip Docker entirely. Run each service in its own LXC container using Proxmox's built-in container management.

| Pros | Cons |
|---|---|
| Lowest per-service overhead | No Docker Compose — lose the ecosystem |
| Each service independently snapshotable | Manual dependency management (no `docker pull`) |
| Proxmox UI manages everything | Upgrades are per-LXC `apt upgrade` — tedious for 10+ services |
| No Docker daemon overhead (~50MB) | No standard image registry — must build from scratch or use turnkey |
| | Ansible roles need to install each service natively (not just compose up) |
| | Community support assumes Docker; debugging is harder |

**Verdict:** Makes sense for 2-3 simple services. Falls apart at 10+ services where Docker Compose's declarative model and image registry save massive time.

### Memory comparison (for a single service like PostgreSQL)

| Approach | Approximate RAM overhead |
|---|---|
| Docker in LXC (Option B) | ~30MB (LXC init) + ~50MB (dockerd) + service |
| Docker in VM (Option C) | ~500MB (kernel + systemd + drivers) + ~50MB (dockerd) + service |
| Native LXC per service (Option D) | ~30MB (LXC init) + service |
| Docker on host (Option A) | ~50MB (dockerd) + service |

**For our 10+ services, Docker-in-LXC saves ~5GB vs Docker-in-VM** and avoids the operational complexity of Option D.

---

## 2. Docker Compose vs One-LXC-Per-Service

### The upgrade problem

**Docker Compose (our approach):**
```bash
docker compose pull && docker compose up -d
# Downloads new image, recreates container, done.
# Rollback: docker compose up -d  (with previous image tag)
```

**LXC per service:**
```bash
# SSH into the LXC
apt update && apt upgrade
# Hope nothing breaks
# If it does: restore from Proxmox snapshot (downtime)
# Or: create new LXC from template, reinstall, reconfigure
```

### Comparison matrix

| Concern | Docker Compose | LXC per service |
|---|---|---|
| **Upgrade** | `docker compose pull` — atomic, seconds | `apt upgrade` — variable, can break |
| **Rollback** | Change image tag, `compose up` | Restore Proxmox snapshot (slow) |
| **Reproducibility** | Image is immutable, same everywhere | Depends on apt state, manual config |
| **Config management** | `.env` + `compose.yml` — declarative | Ansible role per service (complex) |
| **Memory per service** | Container overhead ~5-10MB | LXC overhead ~30-50MB |
| **Isolation** | Docker network namespace | Full network namespace + cgroups |
| **Monitoring** | `docker stats`, Prometheus exporters | Per-LXC metrics from Proxmox |
| **Shared resources** | Docker networks, shared volumes | Proxmox bridge, NFS |
| **GPU sharing** | `/dev/dri` bind mount in compose | `/dev/dri` in LXC config per container |
| **Community support** | Vast — every service has a `docker-compose.yml` | Small — Proxmox community scripts |
| **Watchtower** | Auto-updates all containers | N/A — no equivalent for LXC |

### Memory impact at our scale

**Docker-in-LXC (current — 2 Docker LXCs):**
- EQ12: 1 LXC × ~80MB overhead + 7 Docker containers
- N5 Pro: 1 LXC × ~80MB overhead + 6 Docker containers
- **Total container overhead: ~160MB**

**LXC-per-service (alternative — 13 LXCs):**
- 13 LXCs × ~30-50MB each = ~400-650MB
- No Docker daemon overhead (saves ~100MB total across 2 hosts)
- **Total container overhead: ~400-650MB**

Docker-in-LXC is more memory-efficient at our scale because the Docker daemon overhead (~50MB per host) is shared across all services, while LXC-per-service pays ~30-50MB per service.

---

## 3. Proxmox Community Scripts vs Self-Managed

[Proxmox VE Helper-Scripts](https://github.com/community-scripts/ProxmoxVE) (formerly tteck scripts) provide one-line installers for 200+ applications in LXC containers.

### What they do

```bash
# Example: install Docker LXC
bash -c "$(wget -qLO - https://github.com/community-scripts/ProxmoxVE/raw/main/ct/docker.sh)"
```

This creates an LXC, installs Docker + Compose, configures networking — all interactively from the Proxmox shell. There are scripts for individual applications too (Immich, Frigate, NextCloud, PostgreSQL, etc.) that create dedicated LXCs with the service pre-installed.

### Comparison

| Concern | Community Scripts | Our Ansible Approach |
|---|---|---|
| **Initial setup speed** | Minutes — one command | Hours — write roles, test, iterate |
| **Reproducibility** | Re-run the script (interactive, may vary) | `ansible-playbook site.yml` — fully deterministic |
| **Version control** | None — scripts run ephemerally | Everything in Git with full history |
| **Customization** | Fork the script, maintain your fork | Change variables in `host_vars/*.yml` |
| **Upgrades** | Re-run script or manual `apt upgrade` | `docker compose pull` — atomic and rollback-friendly |
| **Secrets management** | Manual — type passwords during install | ansible-vault — encrypted, templated, automated |
| **Multi-host** | Run on each host separately | Single inventory, one command for all hosts |
| **Drift detection** | None — no state tracking | Ansible `--check --diff` shows divergence |
| **Service dependencies** | Each script is independent | Compose networks, shared PostgreSQL, coordinated deploys |
| **Maintenance burden** | Low — community maintains scripts | Medium — you maintain roles (but they're simple) |
| **GPU passthrough** | Some scripts support it | Full control via LXC config |
| **Memory model** | 1 LXC per service (30-50MB × N) | 1 LXC with Docker (80MB + shared daemon) |

### When community scripts make sense

- **One-off utilities** — Need a quick Wireguard server? PiHole? Script it in 2 minutes.
- **Exploring new software** — Spin up an LXC to try something, delete when done.
- **Simple, standalone services** — No shared databases, no compose orchestration, no secrets rotation.

### When our Ansible approach is better

- **10+ services with shared dependencies** — PostgreSQL shared across Immich/Joplin/NextCloud.
- **Repeatable, versionable deployments** — Git history shows exactly what changed and when.
- **Multi-host coordination** — EQ12 and N5 Pro configured from one inventory.
- **Secret management** — ansible-vault handles all credentials consistently.
- **Memory-constrained hosts** — EQ12 (16GB) can't afford 30-50MB per LXC × 7 services.

### Hybrid approach (recommended)

Use community scripts for:
- Home Assistant (already VM-100, originally installed via helper script)
- Any future one-off services that don't need Ansible management

Use Ansible + Docker Compose for:
- All services in our `containers/` directory
- Anything that shares a database, needs secrets, or runs on both hosts

The scripts and Ansible are not mutually exclusive. The `tags: [proxmox-helper-scripts]` on VM-100 already documents that Home Assistant was installed this way.

---

## 4. Memory Optimization Strategy

With EQ12 at 16GB and N5 Pro at ~62GB usable (32GB to GPU UMA), memory is a real constraint.

### Current allocation

**EQ12 (16GB total):**

| Resource | RAM | Notes |
|---|---|---|
| Proxmox host | ~1GB | Kernel + ZFS ARC (capped) |
| VM-100 (Home Assistant) | 4GB | HAOS requirement |
| CT-101 (Docker: 7 services) | 4GB | PG, Grafana, Vaultwarden, SearXNG, Joplin, Portainer, Watchtower |
| CT-104 (Nginx Proxy Manager) | 512MB | Lightweight reverse proxy |
| **Remaining** | **~6.5GB** | ZFS ARC cache, headroom |

**N5 Pro (62GB usable):**

| Resource | RAM | Notes |
|---|---|---|
| Proxmox host | ~2GB | Kernel + ZFS ARC |
| VM-200 (TrueNAS) | 16GB | ZFS requires RAM — 1GB per TB rule of thumb |
| CT-201 (Docker: 6 services) | 24GB | Immich, Frigate, NextCloud, PG, Portainer, Watchtower |
| **Remaining** | **~20GB** | ZFS ARC, future services |

### Optimization techniques

**1. Share services across containers instead of duplicating:**
- One PostgreSQL per Docker LXC, shared by Immich + Joplin + NextCloud via Docker network
- One Watchtower per Docker LXC, monitors all containers
- One Portainer per Docker LXC, manages all stacks

**2. Set Docker memory limits in compose files:**
```yaml
services:
  immich-server:
    deploy:
      resources:
        limits:
          memory: 4G
```
This prevents a single service from consuming all available RAM.

**3. Tune ZFS ARC:**
```bash
# /etc/modprobe.d/zfs.conf
# EQ12: Cap ARC at 2GB (default would consume ~8GB)
options zfs zfs_arc_max=2147483648

# N5 Pro: Cap ARC at 4GB
options zfs zfs_arc_max=4294967296
```
ZFS ARC is the single biggest hidden memory consumer on Proxmox. Default behavior is to use 50% of total RAM.

**4. Use Alpine-based Docker images where available:**
- `postgres:16-alpine` vs `postgres:16` — saves ~100MB per container
- `redis:7-alpine` vs `redis:7` — saves ~50MB

**5. Disable unused Proxmox services:**
```bash
systemctl disable --now pve-ha-lrm pve-ha-crm  # If not using HA cluster
systemctl disable --now spiceproxy              # If not using SPICE
```
Saves ~100-200MB on the host.

**6. Right-size LXC allocations:**
Use `docker stats` after services stabilize to see actual usage, then adjust LXC memory limits:
```bash
# Inside CT-101:
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"
```

### Where NOT to save memory

- **TrueNAS (VM-200):** 16GB is the minimum for ZFS with 130TB raw. Don't reduce.
- **Immich ML:** Machine learning model loading is memory-intensive. 2-4GB is typical.
- **PostgreSQL shared_buffers:** Default 128MB is too low if 3+ databases are active. Set to 512MB-1GB.

---

## 5. Decision Summary

| Decision | Choice | Rationale |
|---|---|---|
| Container runtime | Docker in LXC | Best memory/overhead ratio at 10+ services |
| Service isolation | Compose stacks in shared Docker LXC | Shared daemon, shared PG, minimal overhead |
| Orchestration | Ansible + Docker Compose | Reproducible, versionable, multi-host |
| Community scripts | Hybrid — scripts for one-offs, Ansible for managed services | Best of both worlds |
| GPU sharing | VAAPI device passthrough in LXC | Shared `/dev/dri` — no full PCI passthrough needed |
| Memory strategy | Capped ZFS ARC + shared services + compose limits | Every GB counts on 16GB EQ12 |

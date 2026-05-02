# Minisforum N5 Pro

## System

- **Model**: Micro Computer (HK) Tech Limited — N5 PRO ("NAS Series")
- **SKU**: MGF8NAA
- **Serial**: MD················47
- **OS**: Proxmox VE 9.1.6 (pve-manager 9.1.9)
- **Kernel**: 6.17.13-2-pve

## CPU

- **Model**: AMD Ryzen AI 9 HX PRO 370 w/ Radeon 890M
- **Architecture**: x86_64 (Zen 5)
- **Cores / Threads**: 12 / 24
- **Frequency**: 2000 MHz base → 5157 MHz boost
- **L3 Cache**: 32 MiB
- **Virtualization**: AMD-V

## Memory

- **Total**: 96 GB DDR5-5600 ECC
- **DIMMs**: 2× Micron MB48G56S80M2R8 (48 GB each, 4800 MT/s)
- **GPU UMA allocation**: 32 GB (configured in BIOS)
- **Available to Proxmox**: ~62 GB

## Storage

### NVMe

| Device | Model                               | Capacity | PCI      | IOMMU Group | Role                               |
| ------ | ----------------------------------- | -------- | -------- | ----------- | ---------------------------------- |
| nvme0  | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB     | `c6:00.0` | 22          | TrueNAS mirrored special vdev      |
| nvme1  | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB     | `c3:00.0` | 19          | TrueNAS mirrored special vdev      |
| nvme2  | WD Black SN850X 4TB (`WDS400T2X0E`) | 4 TB     | `c2:00.0` | 18          | Proxmox boot / ZFS `rpool`         |

### SATA HDD (via JMicron JMB58x controller)

| Device | Model                      | Capacity | Serial   | RPM  |
| ------ | -------------------------- | -------- | -------- | ---- |
| sda    | Seagate Exos ST26000NM000C | 26 TB    | ZX····YW | 7200 |
| sdb    | Seagate Exos ST26000NM000C | 26 TB    | ZX····VK | 7200 |
| sdc    | Seagate Exos ST26000NM000C | 26 TB    | ZX····RP | 7200 |
| sdd    | Seagate Exos ST26000NM000C | 26 TB    | ZX····NC | 7200 |
| sde    | Seagate Exos ST26000NM000C | 26 TB    | ZX····GS | 7200 |

**Total raw HDD capacity**: 130 TB (5× 26 TB)

### ZFS

- **Pool**: `rpool` on nvme0 (4 TB NVMe)
- **Total**: 3.59 TiB
- **Used**: 4.23 GiB

### Proxmox Storage

| Name      | Type    | Status |
| --------- | ------- | ------ |
| local     | dir     | active |
| local-zfs | zfspool | active |

## GPU

- **Model**: AMD Radeon 890M (Strix)
- **PCI Address**: `c7:00.0`
- **Device ID**: `1002:150e`
- **IOMMU Group**: 23 (isolated — only GPU in this group)
- **VRAM**: Shares UMA allocation (32 GB from system memory)
- **Audio**: HDMI/DP audio at `c7:00.1` — Device ID `1002:1640`, IOMMU group 24

## NPU

- **Model**: AMD Strix Halo Neural Processing Unit
- **PCI Address**: `c8:00.1`
- **Device ID**: `1022:17f0`
- **IOMMU Group**: 30

## Network

| Interface | Controller       | Speed  | Bridge | IP                 | Status |
| --------- | ---------------- | ------ | ------ | ------------------ | ------ |
| nic0      | Realtek RTL8126  | 5 GbE  | vmbr0  | DHCP (from Mac)    | UP     |
| nic1      | Aquantia AQC113  | 10 GbE | vmbr1  | 192.168.30.5/18    | UP     |
| —         | host-only        | N/A    | vmbr2  | 10.99.99.1/24      | UP     |

- **vmbr0** (nic0 / 5GbE): DHCP — connected to Mac for internet sharing, metric 100
- **vmbr1** (nic1 / 10GbE): Static 192.168.30.5/18 — gateway 192.168.23.1, metric 200
- **vmbr2** (host-only): No physical NIC — isolated bridge for NFS traffic between TrueNAS and Docker LXC. Paravirtualized VirtIO processes at RAM speed. Managed by Ansible (`proxmox_host` role).
- **Cross-host**: Direct LAN to EQ12 via vmbr1

## IOMMU Groups (key groups for passthrough)

| Group | Devices                                    | Notes                                     |
| ----- | ------------------------------------------ | ----------------------------------------- |
| 17    | JMicron JMB58x SATA controller (`c1:00.0`) | Pass to TrueNAS for direct HDD access     |
| 18    | WD SN850X 4TB NVMe (`c2:00.0`)             | Boot drive — do not pass through          |
| 19    | WD SN850X 2TB NVMe (`c3:00.0`)             | TrueNAS mirrored special vdev             |
| 22    | WD SN850X 2TB NVMe (`c6:00.0`)             | TrueNAS mirrored special vdev             |
| 23    | AMD Radeon 890M GPU (`c7:00.0`)            | Isolated — clean passthrough              |
| 24    | AMD HDMI/DP audio (`c7:00.1`)              | Pair with GPU for video+audio passthrough |
| 30    | AMD Strix Halo NPU (`c8:00.1`)             | AI accelerator                            |

## Planned Workloads

### VM 200: TrueNAS

- NAS / storage server
- Pass through JMicron JMB58x SATA controller (IOMMU group 17) for direct access to 5× 26TB HDDs
- Pass through 2× WD SN850X 2TB NVMe (IOMMU groups 19 and 22) for mirrored ZFS special vdev
- Boot disk on ZFS local-zfs
- net0: vmbr1 (10GbE LAN), net1: vmbr2 (host-only NFS at 10.99.99.2)
- Boots first (`startup: order=1`)

### CT 201: Docker Host

- Immich, Frigate, NextCloud, PostgreSQL, Lyrion Music Server
- 8 cores, 24 GB RAM, Ubuntu 24.04
- GPU access via `/dev/dri` + `/dev/kfd` bind-mount (VAAPI + ROCm)
- ROCm userspace installed by `docker_host` role
- net0: vmbr1 (10GbE LAN at 192.168.30.15), net1: vmbr2 (host-only NFS at 10.99.99.3)
- NFS feature enabled for Docker NFS volume driver
- Boots after TrueNAS (`startup: order=2, up=60`)
- Will share centralized monitoring with EQ12

### GPU Passthrough VM (TBD)

- 32 GB GPU memory available via UMA
- GPU PCI `c7:00.0` (`1002:150e`) in IOMMU group 23
- HDMI audio PCI `c7:00.1` (`1002:1640`) in IOMMU group 24
- Intended for AI / media processing
- **Note**: cannot run simultaneously with LXC GPU sharing (see GPU Passthrough below)

### NPU Passthrough (TBD)

- NPU PCI `c8:00.1` (`1022:17f0`) in IOMMU group 30
- Could be passed to GPU VM or dedicated workload

## GPU Passthrough

### Architecture

Kernel 6.17+ has the inbox `amdgpu` driver with full Strix Point (gfx1150) support.
The Proxmox host does **not** need ROCm installed — only firmware and udev rules.
ROCm userspace libraries are installed inside the LXC container only.

```text
┌───────────────────────────────────────────────────────┐
│  Proxmox Host (n5pro)                                 │
│  Managed by: proxmox_host role (gpu_sharing.enabled)  │
│                                                       │
│  amdgpu kernel driver ← inbox, loaded automatically   │
│  pve-firmware ← apt package                           │
│  udev rules (/etc/udev/rules.d/70-amdgpu.rules)       │
│  /dev/dri/card0, /dev/dri/renderD128, /dev/kfd        │
│         │              │              │               │
│         ▼              ▼              ▼               │
│  ┌──── bind-mount ──── bind-mount ── bind-mount ──┐   │
│  │  CT 201 (n5pro-docker) — Ubuntu 24.04          │   │
│  │  Managed by: docker_host role (gpu_sharing)    │   │
│  │                                                │   │
│  │  amdgpu-install --usecase=rocm,hip,mllib       │   │
│  │                 --no-dkms (userspace only)     │   │
│  │                                                │   │
│  │  Docker containers:                            │   │
│  │    --device /dev/dri --device /dev/kfd         │   │
│  │    -e HSA_OVERRIDE_GFX_VERSION=11.5.0          │   │
│  └────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
```

### What Gets Installed Where

| Layer                 | What                                             | Managed by                                                     |
| --------------------- | ------------------------------------------------ | -------------------------------------------------------------- |
| **Proxmox host**      | `pve-firmware` + udev rules                      | `proxmox_host` role (`gpu_sharing.enabled`)                    |
| **LXC config**        | `/dev/dri` + `/dev/kfd` bind-mount, cgroup allow | `proxmox_guests` role (`gpu_sharing: true` on LXC)             |
| **Inside LXC**        | `amdgpu-install --usecase=<list> --no-dkms`      | `docker_host` role (`gpu_sharing.enabled` + `rocm_usecases`)   |
| **Docker containers** | `--device /dev/dri --device /dev/kfd`            | Per-service compose files                                      |

### ROCm Usecases (Flag-Gated)

The `gpu_sharing.rocm_usecases` list in host_vars controls what gets installed:

| Usecase | What it adds                             | When to use                      |
| ------- | ---------------------------------------- | -------------------------------- |
| `rocm`  | Base runtime (rocm-smi, rocminfo, VAAPI) | Always — minimum for GPU access  |
| `hip`   | HIP runtime + compiler                   | AI/ML inference (Ollama, vLLM)   |
| `mllib` | rocBLAS, MIOpen, etc.                    | Full ML training/inference       |

Example in `host_vars/n5pro_docker/vars.yml`:

```yaml
gpu_sharing:
  enabled: true
  rocm_usecases: [rocm, hip, mllib]  # full ML stack
```

### ROCm Version Pinning

ROCm version is controlled by variables in `ansible/roles/docker_host/defaults/main.yml`:

```yaml
rocm_version: "7.2.2"           # installer .deb version
rocm_build: "70202"             # build suffix in .deb filename
rocm_graphics_version: "7.2.1"  # AMD quirk: graphics repo != installer version
```

The AMD quick-start guide requires a `sed` fix because the 7.2.2 installer creates a
`graphics/7.2.2` repo entry, but packages are published under `graphics/7.2.1`.
This is handled automatically by the `docker_host` role.

Bump all three variables when upgrading to a new ROCm release.

### HSA_OVERRIDE_GFX_VERSION

The Radeon 890M is gfx1150 (Strix Point). ROCm may not recognize it without a hint:

```bash
export HSA_OVERRIDE_GFX_VERSION=11.5.0  # or 11.5.1 — test both
```

This is set system-wide via `/etc/profile.d/rocm.sh` inside the LXC (managed by Ansible).
For Docker containers, pass it as `-e HSA_OVERRIDE_GFX_VERSION=11.5.0`.

### LXC Config Entries (Managed by Ansible)

The `proxmox_guests` role adds these to `/etc/pve/lxc/201.conf`:

```text
# DRI devices — VAAPI hardware video encoding/decoding
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
# KFD device — AMD ROCm compute interface
lxc.cgroup2.devices.allow: c <kfd_major>:<kfd_minor> rwm
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file
```

### References

- Ansible host config: `ansible/inventory/host_vars/n5pro/vars.yml`
- Ansible LXC GPU task: `ansible/roles/proxmox_guests/tasks/lxc-gpu-passthrough.yml`
- Ansible ROCm install: `ansible/roles/docker_host/tasks/main.yml`
- ROCm version defaults: `ansible/roles/docker_host/defaults/main.yml`
- Proxmox wiki: [PCI Passthrough](https://pve.proxmox.com/wiki/PCI_Passthrough)
- ROCm docs: [ROCm installation guide](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/)
- Legacy setup scripts: <https://github.com/mutovkin/proxmox-gpu-setup-scripts> (no longer needed for host setup)

## NFS Architecture

Containers that need TrueNAS storage (Lyrion, Frigate, etc.) use Docker NFS volumes instead of `/etc/fstab` mounts. This is a deliberate choice to avoid a boot-order race condition:

1. **Dependency inversion** — The NFS mount is bound to the container's lifecycle, not the host boot sequence. Docker handles the mount when starting the container.
2. **Fail-safe booting** — If TrueNAS isn't ready when a container starts, the NFS mount fails and the container crashes immediately. This prevents Lyrion from booting against an empty directory and corrupting its database.
3. **Self-healing** — `restart: unless-stopped` causes Docker to continuously retry. Once TrueNAS finishes booting and exports the NFS share, the next restart succeeds.

```text
┌─────────────────────────────────────────────────────────────┐
│  Proxmox Host (n5pro)                                       │
│                                                             │
│  vmbr2 — host-only bridge (no physical NIC)                 │
│  10.99.99.1/24 — host IP                                    │
│       │                    │                                 │
│       ▼                    ▼                                 │
│  VM 200 (TrueNAS)     CT 201 (Docker LXC)                   │
│  10.99.99.2            10.99.99.3                            │
│       │                    │                                 │
│       │   NFS over vmbr2    │                                │
│       └────────────────────►│                                │
│                    Docker NFS volume driver                   │
│                         │                                    │
│                         ▼                                    │
│                   Lyrion container (/music:ro)               │
└─────────────────────────────────────────────────────────────┘
```

**Boot order:** TrueNAS (order=1) boots first. Docker LXC (order=2, up=60) waits 60 seconds before starting, giving TrueNAS time to initialize its network stack and export NFS shares.

## VM/LXC Definitions

Defined in `ansible/inventory/host_vars/n5pro/vars.yml`:

| ID  | Type | Name          | Cores | RAM   | Storage                  | Notes                                                  |
| --- | ---- | ------------- | ----- | ----- | ------------------------ | ------------------------------------------------------ |
| 200 | VM   | truenas       | 4     | 16 GB | 32 GB boot               | UEFI/q35, SATA+NVMe PCI passthrough, dual NIC, boot=1  |
| 201 | CT   | n5pro-docker  | 8     | 24 GB | 8 GB root + 200 GB /data | Ubuntu 24.04, nesting, GPU, NFS, dual NIC, boot=2      |

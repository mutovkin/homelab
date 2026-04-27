# Minisforum N5 Pro

## System

- **Model**: Micro Computer (HK) Tech Limited — N5 PRO ("NAS Series")
- **SKU**: MGF8NAA
- **Serial**: MD················47
- **OS**: Proxmox VE 9.1.6 (pve-manager 9.1.6)
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

| Device | Model | Capacity | Role |
|--------|-------|----------|------|
| nvme0 | WD Black SN850X 4TB (`WDS400T2X0E`) | 4 TB | Proxmox boot / ZFS `rpool` |
| nvme1 | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB | Available |
| nvme2 | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB | Available |

### SATA HDD (via JMicron JMB58x controller)

| Device | Model | Capacity | Serial | RPM |
|--------|-------|----------|--------|-----|
| sda | Seagate Exos ST26000NM000C | 26 TB | ZX····YW | 7200 |
| sdb | Seagate Exos ST26000NM000C | 26 TB | ZX····VK | 7200 |
| sdc | Seagate Exos ST26000NM000C | 26 TB | ZX····RP | 7200 |
| sdd | Seagate Exos ST26000NM000C | 26 TB | ZX····NC | 7200 |
| sde | Seagate Exos ST26000NM000C | 26 TB | ZX····GS | 7200 |

**Total raw HDD capacity**: 130 TB (5× 26 TB)

### ZFS

- **Pool**: `rpool` on nvme0 (4 TB NVMe)
- **Total**: 3.59 TiB
- **Used**: 4.23 GiB

### Proxmox Storage

| Name | Type | Status |
|------|------|--------|
| local | dir | active |
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

| Interface | Controller | Speed | Bridge | IP | Status |
|-----------|-----------|-------|--------|-----|--------|
| nic0 | Realtek RTL8126 | 5 GbE | vmbr0 | DHCP (from Mac) | UP |
| nic1 | Aquantia AQC113 | 10 GbE | vmbr1 | 192.168.30.5/18 | UP |

- **vmbr0** (nic0 / 5GbE): DHCP — connected to Mac for internet sharing, metric 100
- **vmbr1** (nic1 / 10GbE): Static 192.168.30.5/18 — gateway 192.168.23.1, metric 200
- **Cross-host**: Direct LAN to EQ12 via vmbr1

## IOMMU Groups (key groups for passthrough)

| Group | Devices | Notes |
|-------|---------|-------|
| 17 | JMicron JMB58x SATA controller (`c1:00.0`) | Pass to TrueNAS for direct HDD access |
| 23 | AMD Radeon 890M GPU (`c7:00.0`) | Isolated — clean passthrough |
| 24 | AMD HDMI/DP audio (`c7:00.1`) | Pair with GPU for video+audio passthrough |
| 30 | AMD Strix Halo NPU (`c8:00.1`) | AI accelerator |

## Planned Workloads

### VM 200: TrueNAS

- NAS / storage server
- Pass through JMicron JMB58x SATA controller (IOMMU group 17) for direct access to 5× 26TB HDDs
- Boot disk on ZFS local-zfs

### CT 201: Docker Host

- Immich, Frigate, NextCloud, PostgreSQL
- 8 cores, 24 GB RAM, Debian 13
- GPU access via `/dev/dri` + `/dev/kfd` bind-mount (VAAPI + ROCm)
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

### Two Approaches (Mutually Exclusive Per GPU)

There are two fundamentally different ways to give guests access to the Radeon 890M:

| | VFIO-PCI (VM exclusive) | Device sharing (LXC) |
|---|---|---|
| **How it works** | `vfio-pci` kernel driver claims the GPU at boot; the host has no `/dev/dri` | Host keeps `amdgpu` driver loaded; `/dev/dri` + `/dev/kfd` bind-mounted into containers |
| **Guest type** | VMs only (QEMU/KVM) | LXC containers (can share across multiple CTs) |
| **Performance** | Near-native (bare-metal GPU in VM) | Near-native for VAAPI; ROCm compute works but with shared scheduling |
| **Use case** | Dedicated AI VM, Windows GPU VM, gaming | Frigate (VAAPI), Immich ML (ROCm), multi-service Docker |
| **ROCm on host?** | Not needed (GPU is owned by VM) | **Required** — host must have amdgpu + ROCm userspace |
| **ROCm in guest?** | Full install inside VM | Userspace libraries only (no DKMS/kernel modules) |
| **Config in Ansible** | `gpu_passthrough.enabled: true` in host_vars | `gpu_passthrough: true` on LXC definition in `proxmox_lxcs` |

**Current choice: LXC device sharing** — CT 201 needs GPU for Frigate and Immich.

### Where ROCm Gets Installed

ROCm is required at **two layers** for LXC GPU sharing:

```
┌─────────────────────────────────────────────────────┐
│  Proxmox Host (n5pro)                               │
│                                                     │
│  amdgpu kernel driver ← loaded automatically        │
│  ROCm userspace (rocm-smi, rocminfo, rocm-libs)     │
│  udev rules (/etc/udev/rules.d/99-gpu-passthrough)  │
│  /dev/dri/card0, /dev/dri/renderD128, /dev/kfd      │
│         │              │              │              │
│         ▼              ▼              ▼              │
│  ┌──── bind-mount ──── bind-mount ── bind-mount ──┐ │
│  │  CT 201 (n5-docker)                            │ │
│  │                                                │ │
│  │  ROCm userspace only (no DKMS)                 │ │
│  │  Docker containers use --device /dev/dri       │ │
│  │  + --device /dev/kfd for ROCm compute          │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Host setup** (run once on the Proxmox host via SSH):
```bash
cd /root/proxmox-gpu-setup-scripts
./host/003\ -\ install-amd-drivers.sh     # ROCm 7.2 + amdgpu
./host/005\ -\ verify-amd-drivers.sh      # Verify: rocm-smi, rocminfo
./host/007\ -\ setup-udev-gpu-rules.sh    # Persistent /dev/dri permissions
```

**LXC setup** (run inside the container):
```bash
cd /root/proxmox-gpu-setup-scripts
./lxc/install-docker-and-amd-drivers-in-lxc.sh  # Docker + ROCm userspace
```

Key ROCm environment variables (set automatically by the scripts):
```bash
export HSA_OVERRIDE_GFX_VERSION=11.5.1  # Required for gfx1150 (Radeon 890M)
export HSA_ENABLE_SDMA=0                # APU stability workaround
```

### LXC Config Entries (Managed by Ansible)

The `proxmox_guests` role adds these to `/etc/pve/lxc/201.conf`:
```
lxc.cgroup2.devices.allow: c 226:* rwm          # /dev/dri/*
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
```

For full ROCm compute (not just VAAPI), `/dev/kfd` is also needed:
```
lxc.cgroup2.devices.allow: c <kfd_major>:<kfd_minor> rwm
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file
```

### Switching to VFIO-PCI (VM Exclusive)

If you later want a dedicated GPU VM instead of LXC sharing:

1. In `ansible/inventory/host_vars/n5pro/vars.yml`:
   - Set `gpu_passthrough.enabled: true`
   - Uncomment `vfio_pci_ids: "1002:150e,1002:1640"`
2. Remove `gpu_passthrough: true` from CT 201's LXC definition
3. Re-run the playbook — this will configure GRUB + VFIO modules + initramfs
4. Reboot the host (VFIO-PCI binds at boot, requires reboot)
5. Add a VM with `hostpci` mapping to the GPU PCI addresses

### References

- Setup scripts: https://github.com/mutovkin/proxmox-gpu-setup-scripts
  - Forked from [eikaramba/proxmox-setup-scripts](https://github.com/eikaramba/proxmox-setup-scripts)
- Ansible config: `ansible/inventory/host_vars/n5pro/vars.yml` — GPU vars + comments
- Ansible LXC GPU task: `ansible/roles/proxmox_guests/tasks/lxc-gpu-passthrough.yml`
- Proxmox wiki: [PCI Passthrough](https://pve.proxmox.com/wiki/PCI_Passthrough)
- ROCm docs: [ROCm installation guide](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/)

## VM/LXC Definitions

Defined in `ansible/inventory/host_vars/n5pro/vars.yml`:

| ID  | Type | Name      | Cores | RAM    | Storage    | Notes |
|-----|------|-----------|-------|--------|------------|-------|
| 200 | VM   | truenas   | 4     | 16 GB  | 32 GB boot | UEFI/q35, SATA controller PCI passthrough |
| 201 | CT   | n5-docker | 8     | 24 GB  | 8 GB root + 200 GB /data | Debian 13, nesting, GPU passthrough |

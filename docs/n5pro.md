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

- Service placement TBD
- Will share centralized monitoring with EQ12

### GPU Passthrough VM (TBD)

- 32 GB GPU memory available via UMA
- GPU PCI `c7:00.0` (`1002:150e`) in IOMMU group 23
- HDMI audio PCI `c7:00.1` (`1002:1640`) in IOMMU group 24
- Intended for AI / media processing

### NPU Passthrough (TBD)

- NPU PCI `c8:00.1` (`1022:17f0`) in IOMMU group 30
- Could be passed to GPU VM or dedicated workload

## VM/LXC Definitions

Defined in `ansible/inventory/host_vars/n5pro/vars.yml`:

| ID  | Type | Name      | Cores | RAM    | Storage    | Notes |
|-----|------|-----------|-------|--------|------------|-------|
| 200 | VM   | truenas   | 4     | 16 GB  | 32 GB boot | UEFI/q35, SATA controller PCI passthrough |
| 201 | CT   | n5-docker | 8     | 24 GB  | 8 GB root + 200 GB /data | Debian 13, nesting, GPU passthrough |

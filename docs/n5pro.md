# Minisforum N5 Pro

> Hardware inventory below (CPU, memory, storage, ZFS) as enumerated 2026-08-18.

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
| nvme0  | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB     | `c6:00.0` | 22         | TrueNAS mirrored special vdev      |
| nvme1  | WD Black SN850X 2TB (`WDS200T2X0E`) | 2 TB     | `c3:00.0` | 19         | TrueNAS mirrored special vdev      |
| nvme2  | WD Black SN850X 4TB (`WDS400T2X0E`) | 4 TB     | `c2:00.0` | 18         | Proxmox boot / ZFS `rpool`         |

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

- **Pool**: `rpool` on nvme2 (4 TB NVMe — the only NVMe the Proxmox host keeps, since
  nvme0/nvme1 are PCI-passed-through to VM 200, so it enumerates as `nvme0n1` in `lsblk`)
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

The cluster PCI mappings these use (`truenas_sata`, `truenas_nvme0`,
`truenas_nvme1`) are defined in `ansible/inventory/host_vars/n5pro/vars.yml`
(`proxmox_pci_mappings`) and reconciled by the `proxmox_guests` role — update
them there after any hardware re-seat that changes a path or IOMMU group.

## Planned Workloads

### VM 200: TrueNAS

- NAS / storage server
- Pass through JMicron JMB58x SATA controller (IOMMU group 17) for direct access to 5× 26TB HDDs
- Pass through 2× WD SN850X 2TB NVMe (IOMMU groups 19 and 22) for mirrored ZFS special vdev
- Boot disk on ZFS local-zfs
- net0: vmbr1 (10GbE LAN), net1: vmbr2 (host-only NFS at 10.99.99.2)
- Boots first (`startup: order=1,up=180`) — the `up=180` delay holds the docker
  LXC until TrueNAS has finished its slow boot (PCIe passthrough + ZFS import +
  NFS start), so the NFS provider is serving before its consumer starts

### CT 201: Docker Host

- Lyrion Music Server (LMS), Portainer, Watchtower
  (planned #91: Immich, Frigate, NextCloud, PostgreSQL)
- 8 cores, 24 GB RAM, Ubuntu 24.04
- GPU access via `/dev/dri` + `/dev/kfd` bind-mount (VAAPI + ROCm)
- ROCm userspace installed by `docker_host` role
- net0: vmbr1 (10GbE LAN at 192.168.30.15), net1: vmbr2 (host-only NFS at 10.99.99.3)
- NFS feature enabled for Docker NFS volume driver
- Boots after TrueNAS (`startup: order=2`)
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

#### AppArmor for Docker

Privileged LXC containers (`unprivileged: false`) running Docker require their AppArmor profile to be unconfined. Otherwise, Proxmox enforces a restrictive default profile that blocks Docker's `apparmor_parser` when it attempts to load its `docker-default` profile, preventing containers from starting.

```text
lxc.apparmor.profile: unconfined
```

#### GPU Passthrough

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

## Host metrics (telegraf, #186)

Until #186 this machine exported **zero** non-`vector_*` series. That was the
larger of the two gaps: 96 GB of RAM, twelve Zen 5 cores, the TrueNAS VM and the
GPU-sharing CT 201 all ran with no CPU, memory, load, uptime or thermal telemetry
whatsoever. It now runs a native, pinned telegraf
([`roles/telegraf_agent`](../ansible/roles/telegraf_agent/)) that remote-writes to
VictoriaMetrics on eq12_docker, stamped `host="n5pro"` — the fleet-wide
`host = inventory_hostname` convention from #178.

CT 201 (`n5pro_docker`) is **not** covered by this. It still has no `docker_*`
series of its own; a second docker-metrics collector is a separate decision.

**Collected:** `cpu` (per-core and total), `mem`, `swap`, `disk`, `diskio`,
`net`, `netstat`, `kernel`, `processes`, `system` (load/uptime), `zfs` (ARC
kstats from `/proc/spl/kstat/zfs`), `sensors` (below), and `smart` (NVMe wear,
available spare, power-on hours, media errors — 14 `smart_device_*` series).
Interval 60 s.

**Power** is partially covered: `amdgpu`'s `power1_average` / `power1_input` come
through the `sensors` input, so iGPU package draw is measured. CPU-package RAPL
(`energy_uj`) is **not** — it is deferred to #194, which the agent's privilege
model already accommodates as a config addition. No fan input exists here at all;
see the negative result below.

### Measured sensor surface

`hwmon` chips present: `acpitz`, `r8169_0_c500:00`, `nic1`, `nvme`, `k10temp`,
`spd5118` ×2, `amdgpu`. Enumerated 2026-08-24. Note the `hwmon` numbering has
gaps (hwmon3/4 absent) — never key anything on the index.

| Source | Readings |
| ------ | -------- |
| `k10temp-pci-00c3` | `Tctl` 39.75 °C — **one value, no per-core temps** (unlike EQ12's coretemp) |
| `spd5118-i2c-0-50` / `-51` | **DDR5 DIMM temperatures**, 36.5 °C each, `max` 55, `crit` 85 — two modules |
| `nic1-pci-c400` | PHY 44 °C, MAC 44 °C |
| `r8169_0_c500:00-mdio-0` | 38.5 °C, `max` 120 |
| `amdgpu-pci-c700` | edge temp 36 °C; `power1_average`/`power1_input` 7.07 W; `sclk` 600 MHz |
| `acpitz-acpi-0` | 20.0 °C |
| `nvme-pci-c200` | Composite 33.85 °C, `max` 89.85, `crit` 93.85 |

Nine `sensors_temp<N>_input` series reach the store, across all eight chips. As on
EQ12 the digit is part of the metric name (`sensors_temp1_input` ..), because
`remove_numbers = false` is set deliberately.

Two things worth carrying into any dashboard or alert built on this:

- **`k10temp` gives one `Tctl`, not per-core.** A panel written against EQ12's
  five `coretemp` readings shows empty for this host unless it handles both
  shapes. `Tctl` on AMD can also carry an offset, so nothing should put a
  threshold on it without a second source to check against.
- **`amdgpu` is the only host-side view of CT 201's GPU load.** `/dev/dri` is
  device-shared into the container for VAAPI, so iGPU edge temperature and
  package power are what that workload costs, measured from here.

`amdgpu`'s `vddgfx` and `vddnb` both read **0.000 V** — an unsupported reading,
not a measurement. They are dropped at the source by a `[inputs.sensors.tagdrop]`
filter, and every deploy asserts against telegraf's own output that they never
appear. Zero `vdd*` series exist in the store for this host, and that is
deliberate: a fabricated zero drags `min()` and `avg()` panels to the floor, which
this repo has been bitten by before.

### Fan speed: a definitive negative

**This board has no fan interface of any kind.** Not a tachometer, not even
binary on/off state — nothing. This negative still stands after the 2026-08-25
round of probing that overturned the equivalent finding on EQ12; the two hosts
genuinely differ, and the reason is below.

Direct enumeration, 2026-08-24: no `fan*_input` and no `pwm*` under
`/sys/class/hwmon/hwmon*/`, no fan-capable hwmon chip, no Super-I/O-class fan
driver loaded — `k10temp` is the only CPU-temp module (`amdgpu` and `spd5118` are
loaded hwmon modules too, but neither is a fan driver) — and, unlike EQ12, **no
ACPI fan objects at all**.

A deliberate probe followed, run through Ansible as an ad-hoc command rather than
by hand, with host snapshots taken first and `/etc/modules` left unchanged:

```bash
sensors-detect --auto    # MEASURED 2026-08-25
```

Its verdict, verbatim: **"Sorry, no sensors were detected."** The Super-I/O scan
surfaced one chip ID, **`0x5571`**, which the kernel does not recognise — and the
I²C scan found nothing beyond the `spd5118` DIMM pair that is already driven.

**The chip is very probably an ITE IT5571** (sometimes written IT5571VG). That
identification is not ours and not from a datasheet — ITE publishes none — but
from community reports on this exact machine (ServeTheHome, Level1Techs,
r/MINISFORUM, Unraid forums, late 2025 through 2026), which consistently show the
same unknown-ID symptom across Unraid, Proxmox and CachyOS. **The raw `0x5571` is
kept above deliberately**: that is what `sensors-detect` actually prints, and it
is what a future reader will grep for.

#### Why this is a harder negative than "no driver"

A missing driver is a problem someone could fix. This is worse than that.

Community EC RAM dumps on this machine read **mostly zeros** in the registers
where fan speed, temperature and PWM would live. The BIOS/EC handles fan control
**internally and never publishes the values**. So even a perfect, purpose-written
driver would read zeros — there is nothing on the other side of the register to
read. Forced IDs and out-of-tree `it87` builds have both been tried by others on
this hardware and produce **no usable RPM or PWM**.

That is the difference from EQ12, and it is why that host's answer flipped and
this one's did not. There, a chip was *identified* (ITE IT8613E), the stock
driver already supported a close sibling, and the registers held real values —
so a `force_id` binding worked. Here the chip is unsupported **and** the EC
publishes nothing to read. Two independent blockers, either one sufficient.

**RE-CHECK CONDITION — and note it is a VENDOR action, not a Linux one:** revisit
only if Minisforum ships a BIOS/EC update that publishes those registers, or
releases EC documentation. Do **not** go looking in the kernel tree, and do not
re-run `sensors-detect`; no amount of driver work reaches a register the EC never
fills. The command, the date and the verdict are recorded above.

So there is no fan signal to export and nothing pretends otherwise:
`telegraf_agent_acpi_fan_state` is `false` for this host, and the role deploys no
fan script to it. Unlike EQ12 there is not even binary on/off state here — no
ACPI fan objects at all, so there is nothing to collect in any form.

## NFS Architecture

Containers that need TrueNAS storage (Lyrion's music library, media) use Docker NFS volumes instead of `/etc/fstab` mounts. This is a deliberate choice to avoid a boot-order race condition:

1. **Dependency inversion** — The NFS mount is bound to the container's lifecycle, not the host boot sequence. Docker handles the mount when starting the container.
2. **Fail-safe booting** — If TrueNAS isn't ready when a container starts, the NFS mount fails and the container crashes immediately. This prevents Lyrion from booting against an empty directory and corrupting its database.
3. **Self-healing** — `restart: unless-stopped` causes Docker to continuously retry. Once TrueNAS finishes booting and exports the NFS share, the next restart succeeds.

> **Frigate is not one of these containers.** Its recordings live on local disk:
> `FRIGATE_RECORDINGS_DIR` resolves under `data_mount`
> (`roles/services/frigate/templates/env.j2`), which here is CT 201's own ZFS subvol.
> The compose comment claiming an NFS mount from TrueNAS was stale and was corrected in
> #91; moving recordings to TrueNAS would be a deliberate change to that variable plus a
> real mount, not a comment.

```text
┌─────────────────────────────────────────────────────────────┐
│  Proxmox Host (n5pro)                                       │
│                                                             │
│  vmbr2 — host-only bridge (no physical NIC)                 │
│  10.99.99.1/24 — host IP                                    │
│       │                    │                                │
│       ▼                    ▼                                │
│  VM 200 (TrueNAS)     CT 201 (Docker LXC)                   │
│  10.99.99.2            10.99.99.3                           │
│       │                     │                               │
│       │   NFS over vmbr2    │                               │
│       └────────────────────►│                               │
│                    Docker NFS volume driver                 │
│                             │                               │
│                             ▼                               │
│                   Lyrion container (/music:ro)              │
└─────────────────────────────────────────────────────────────┘
```

**Boot order:** TrueNAS (`order=1,up=180`) boots first; Proxmox then waits 180s
before starting the docker LXC (`order=2`), so TrueNAS has time to import ZFS and
export its NFS shares before the consumer starts. The `up=` delay lives on
TrueNAS, not the LXC — Proxmox applies a guest's `up=` delay *before starting the
next guest in order*, so a delay on the LXC would do nothing useful. As a
backstop for an unusually slow TrueNAS boot, the LXC also runs a
`lms-nfs-heal.service` oneshot that waits for NFS then re-ups the lms stack (#36).

## VM/LXC Definitions

Defined in `ansible/inventory/host_vars/n5pro/vars.yml`:

| ID  | Type | Name          | Cores | RAM   | Storage                  | Notes                                                  |
| --- | ---- | ------------- | ----- | ----- | ------------------------ | ------------------------------------------------------ |
| 200 | VM   | truenas       | 4     | 24 GB | 64 GB boot                | UEFI/q35, SATA+NVMe PCI passthrough, dual NIC, boot=1 |
| 201 | CT   | n5pro-docker  | 8     | 24 GB | 64 GB root + 200 GB /data | Ubuntu 24.04, nesting, GPU, NFS, dual NIC, boot=2     |

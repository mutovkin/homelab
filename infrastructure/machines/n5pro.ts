import { LxcContainer } from "../components/lxc-container";
import { VirtualMachine } from "../components/virtual-machine";

const NODE = "n5pro";
const STORAGE = "local-zfs";
const OS_TEMPLATE_DEBIAN13 =
  "local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst";

/**
 * N5 Pro Infrastructure — Minisforum N5 Pro
 *
 * Hardware:
 *   CPU: AMD Ryzen AI 9 HX PRO 370 (12c/24t, up to 5.16 GHz)
 *   RAM: 96GB DDR5-5600 ECC (2× 48GB Micron), 32GB to GPU UMA, ~62GB for Proxmox
 *   NVMe: 1× 4TB + 2× 2TB WD Black SN850X
 *   HDD: 5× 26TB Seagate Exos (via JMicron JMB58x SATA controller)
 *   GPU: AMD Radeon 890M (PCI c7:00.0, IOMMU group 23)
 *   NPU: AMD Strix Halo (PCI c8:00.1, IOMMU group 30)
 *   Net: 10GbE Aquantia AQC113 + 5GbE Realtek RTL8126
 *
 * Proxmox: 9.1.6, ZFS rpool on 4TB NVMe
 * VM IDs: 200+ range to avoid conflicts with EQ12 (100-199)
 */

// --- VM 200: TrueNAS ---
export const truenas = new VirtualMachine("n5pro-truenas", {
  node: NODE,
  vmId: 200,
  name: "truenas",
  cores: 4,
  memory: 16384, // 16GB
  diskSize: 32, // Boot disk — TrueNAS manages its own storage pools
  storage: STORAGE,
  bios: "ovmf", // UEFI for TrueNAS SCALE
  machine: "q35",
  pciDevices: [
    {
      id: "0000:c1:00.0", // JMicron JMB58x SATA controller (IOMMU group 17)
      pcie: true, // 5× 26TB Seagate Exos HDDs attached
    },
  ],
  startOnBoot: true,
  tags: ["truenas", "storage"],
  description:
    "TrueNAS SCALE — NAS and storage management. JMicron SATA controller passed through for direct access to 5× 26TB HDDs.",
});

// --- CT 201: Docker host ---
export const dockerHost = new LxcContainer("n5pro-docker", {
  node: NODE,
  vmId: 201,
  hostname: "n5-docker",
  osTemplate: OS_TEMPLATE_DEBIAN13,
  cores: 4,
  memory: 8192, // 8GB
  rootDiskSize: 8,
  storage: STORAGE,
  mountPoints: [
    {
      storage: STORAGE,
      path: "/data",
      size: 200,
    },
  ],
  nesting: true,
  startOnBoot: true,
  tags: ["docker"],
  description:
    "Docker host for N5 Pro services. Service assignments TBD — configured via Ansible host_vars.",
});

// --- Placeholder for GPU passthrough VM ---
// Uncomment and configure when GPU workloads are decided:
//
// export const gpuVm = new VirtualMachine("n5pro-gpu", {
//   node: NODE,
//   vmId: 210,
//   name: "gpu-workload",
//   cores: 8,
//   memory: 32768,  // 32GB — matches GPU UMA allocation
//   diskSize: 100,
//   storage: STORAGE,
//   bios: "ovmf",
//   machine: "q35",
//   pciDevices: [
//     {
//       id: "0000:c7:00.0",  // AMD Radeon 890M (IOMMU group 23)
//       pcie: true,
//     },
//     {
//       id: "0000:c7:00.1",  // AMD HDMI/DP audio (IOMMU group 24)
//       pcie: true,
//     },
//   ],
//   startOnBoot: false,
//   tags: ["gpu"],
//   description: "GPU passthrough VM for AI/media workloads. Radeon 890M + HDMI audio.",
// });

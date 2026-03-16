import { LxcContainer } from "../components/lxc-container";
import { VirtualMachine } from "../components/virtual-machine";

const NODE = "pve";
const STORAGE = "local-zfs";
// NOTE: Must match the template actually deployed on existing containers.
// Update this after upgrading existing LXCs to Debian 13.
const OS_TEMPLATE =
  "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst";

/**
 * EQ12 Infrastructure — Beelink EQ12 (AZW)
 *
 * Hardware:
 *   CPU: Intel N100 (Alder Lake-N, 4c/4t, up to 3.4 GHz)
 *   RAM: 16GB (single DIMM)
 *   NVMe: 1× 2TB TEAMGROUP TM8FP4002T (via Realtek RTS5762)
 *   Net: 2× Intel I225-V 2.5GbE (enp2s0 active on vmbr0)
 *
 * Proxmox: 9.1.4, ZFS rpool on 2TB NVMe (192.168.25.5)
 *
 * Current inventory:
 *   VM 100  — ha (Home Assistant, QEMU)
 *   CT 101  — deb-docker (Debian 12 LXC, primary Docker host)
 *   CT 102  — ubuntu-docker (Ubuntu 22.04 LXC, stopped)
 *   CT 104  — nginxproxymanager (LXC via proxmox-helper-scripts)
 *
 * NOTE: These resources already exist. Before first `pulumi up`, run:
 *   pulumi import homelab:proxmox:VirtualMachine eq12-homeassistant <resource-id>
 *   pulumi import homelab:proxmox:LxcContainer eq12-deb-docker <resource-id>
 *   etc.
 */

// --- VM 100: Home Assistant ---
export const homeAssistant = new VirtualMachine("eq12-homeassistant", {
  node: NODE,
  vmId: 100,
  name: "ha",
  cores: 2,
  memory: 4096, // 4GB
  diskSize: 32,
  storage: STORAGE,
  startOnBoot: true,
  tags: ["proxmox-helper-scripts", "home-assistant"],
  description: "Home Assistant OS — QEMU VM",
});

// --- CT 101: deb-docker (primary Docker host) ---
export const debDocker = new LxcContainer("eq12-deb-docker", {
  node: NODE,
  vmId: 101,
  hostname: "deb-docker",
  osTemplate: OS_TEMPLATE,
  cores: 2,
  memory: 4096, // 4GB
  rootDiskSize: 8,
  storage: STORAGE,
  mountPoints: [
    {
      storage: STORAGE,
      path: "/data",
      size: 110,
    },
  ],
  nesting: true, // Required for Docker in LXC
  startOnBoot: true,
  tags: ["docker"],
  description:
    "Primary Docker host — runs all container services (observability, postgresql, vaultwarden, etc.)",
});

// --- CT 102: ubuntu-docker ---
export const ubuntuDocker = new LxcContainer("eq12-ubuntu-docker", {
  node: NODE,
  vmId: 102,
  hostname: "ubuntu-docker",
  osTemplate:
    "local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst",
  cores: 1,
  memory: 1024, // 1GB
  rootDiskSize: 8,
  storage: STORAGE,
  nesting: true,
  startOnBoot: false, // Currently stopped
  tags: ["docker"],
  description: "Secondary Docker host (Ubuntu 22.04) — currently unused",
});

// --- CT 104: nginxproxymanager ---
export const nginxProxyManager = new LxcContainer("eq12-npm", {
  node: NODE,
  vmId: 104,
  hostname: "nginxproxymanager",
  osTemplate: OS_TEMPLATE,
  cores: 2,
  memory: 1024, // 1GB
  rootDiskSize: 4,
  storage: STORAGE,
  nesting: false,
  startOnBoot: true,
  tags: ["proxmox-helper-scripts", "reverse-proxy"],
  description:
    "Nginx Proxy Manager — standalone LXC (failed to run inside Docker)",
});

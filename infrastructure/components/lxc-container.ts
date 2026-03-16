import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

export interface LxcContainerArgs {
  /** Proxmox node to create the container on */
  node: pulumi.Input<string>;
  /** Numeric VM ID (e.g., 101) */
  vmId: pulumi.Input<number>;
  /** Container hostname */
  hostname: pulumi.Input<string>;
  /** OS template (e.g., "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst") */
  osTemplate: pulumi.Input<string>;
  /** Number of CPU cores */
  cores: pulumi.Input<number>;
  /** Memory in MB */
  memory: pulumi.Input<number>;
  /** Swap in MB (default: 512) */
  swap?: pulumi.Input<number>;
  /** Root disk size in GB */
  rootDiskSize: pulumi.Input<number>;
  /** Storage pool for the root disk (default: "local-zfs") */
  storage?: pulumi.Input<string>;
  /** Additional mount points: [{storage, mountPoint, size}] */
  mountPoints?: {
    storage?: pulumi.Input<string>;
    path: pulumi.Input<string>;
    size: pulumi.Input<number>;
  }[];
  /** Network bridge (default: "vmbr0") */
  bridge?: pulumi.Input<string>;
  /** Static IPv4 (CIDR) or "dhcp" */
  ipv4?: pulumi.Input<string>;
  /** IPv4 gateway */
  gateway?: pulumi.Input<string>;
  /** Enable nesting (required for Docker in LXC) */
  nesting?: pulumi.Input<boolean>;
  /** Start on boot */
  startOnBoot?: pulumi.Input<boolean>;
  /** Start the container after creation */
  started?: pulumi.Input<boolean>;
  /** Tags for organization */
  tags?: pulumi.Input<string>[];
  /** Description / notes */
  description?: pulumi.Input<string>;
  /** Unprivileged container (default: true) */
  unprivileged?: pulumi.Input<boolean>;
}

export class LxcContainer extends pulumi.ComponentResource {
  public readonly container: proxmox.ct.Container;

  constructor(
    name: string,
    args: LxcContainerArgs,
    opts?: pulumi.ComponentResourceOptions,
  ) {
    super("homelab:proxmox:LxcContainer", name, {}, opts);

    const mountPoints = (args.mountPoints ?? []).map((mp, i) => ({
      volume: `${mp.storage ?? "local-zfs"}:${mp.size}`,
      path: mp.path,
      key: `${i}`,
    }));

    this.container = new proxmox.ct.Container(
      name,
      {
        nodeName: args.node,
        vmId: args.vmId,
        operatingSystem: {
          templateFileId: args.osTemplate,
        },
        cpu: {
          cores: args.cores,
        },
        memory: {
          dedicated: args.memory,
          swap: args.swap ?? 512,
        },
        disk: {
          datastore_id: args.storage ?? "local-zfs",
          size: args.rootDiskSize,
        },
        mountPoints: mountPoints,
        networkInterfaces: [
          {
            name: "eth0",
            bridge: args.bridge ?? "vmbr0",
            ipv4Address: args.ipv4 ?? "dhcp",
            ipv4Gateway: args.gateway,
          },
        ],
        features: {
          nesting: args.nesting ?? false,
        },
        startOnBoot: args.startOnBoot ?? true,
        started: args.started ?? true,
        tags: args.tags,
        description: args.description,
        unprivileged: args.unprivileged ?? true,
      },
      { parent: this },
    );

    this.registerOutputs({
      containerId: this.container.id,
    });
  }
}

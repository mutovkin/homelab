import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

export interface VirtualMachineArgs {
  /** Proxmox node to create the VM on */
  node: pulumi.Input<string>;
  /** Numeric VM ID (e.g., 100) */
  vmId: pulumi.Input<number>;
  /** VM name */
  name: pulumi.Input<string>;
  /** Number of CPU cores */
  cores: pulumi.Input<number>;
  /** Number of CPU sockets (default: 1) */
  sockets?: pulumi.Input<number>;
  /** Memory in MB */
  memory: pulumi.Input<number>;
  /** Boot disk size in GB */
  diskSize: pulumi.Input<number>;
  /** Storage pool for the boot disk (default: "local-zfs") */
  storage?: pulumi.Input<string>;
  /** ISO file for installation (e.g., "local:iso/haos.iso") */
  iso?: pulumi.Input<string>;
  /** Network bridge (default: "vmbr0") */
  bridge?: pulumi.Input<string>;
  /** Network model (default: "virtio") */
  networkModel?: pulumi.Input<string>;
  /** BIOS type: "seabios" or "ovmf" (default: "seabios") */
  bios?: pulumi.Input<string>;
  /** Machine type (default: "q35") */
  machine?: pulumi.Input<string>;
  /** PCI passthrough devices [{id: "0000:XX:XX.X"}] */
  pciDevices?: {
    id: pulumi.Input<string>;
    rombar?: pulumi.Input<boolean>;
    pcie?: pulumi.Input<boolean>;
  }[];
  /** Start on boot */
  startOnBoot?: pulumi.Input<boolean>;
  /** Start the VM after creation */
  started?: pulumi.Input<boolean>;
  /** Tags for organization */
  tags?: pulumi.Input<string>[];
  /** Description / notes */
  description?: pulumi.Input<string>;
  /** Additional disks [{storage, size}] */
  additionalDisks?: {
    storage?: pulumi.Input<string>;
    size: pulumi.Input<number>;
  }[];
}

export class VirtualMachine extends pulumi.ComponentResource {
  public readonly vm: proxmox.vm.VirtualMachine;

  constructor(
    name: string,
    args: VirtualMachineArgs,
    opts?: pulumi.ComponentResourceOptions,
  ) {
    super("homelab:proxmox:VirtualMachine", name, {}, opts);

    const disks = [
      {
        datastoreId: args.storage ?? "local-zfs",
        fileFormat: "raw" as const,
        interface: "scsi0",
        size: args.diskSize,
      },
      ...(args.additionalDisks ?? []).map((d, i) => ({
        datastoreId: d.storage ?? "local-zfs",
        fileFormat: "raw" as const,
        interface: `scsi${i + 1}`,
        size: d.size,
      })),
    ];

    const hostPcis = (args.pciDevices ?? []).map((dev) => ({
      id: dev.id,
      rombar: dev.rombar ?? true,
      pcie: dev.pcie ?? true,
    }));

    this.vm = new proxmox.vm.VirtualMachine(
      name,
      {
        nodeName: args.node,
        vmId: args.vmId,
        name: args.name,
        cpu: {
          cores: args.cores,
          sockets: args.sockets ?? 1,
        },
        memory: {
          dedicated: args.memory,
        },
        disks: disks,
        cdrom: args.iso
          ? {
            fileId: args.iso,
          }
          : undefined,
        networkDevices: [
          {
            bridge: args.bridge ?? "vmbr0",
            model: args.networkModel ?? "virtio",
          },
        ],
        bios: args.bios ?? "seabios",
        machine: args.machine ?? "q35",
        hostPcis: hostPcis.length > 0 ? hostPcis : undefined,
        onBoot: args.startOnBoot ?? true,
        started: args.started ?? true,
        tags: args.tags,
        description: args.description,
      },
      { parent: this },
    );

    this.registerOutputs({
      vmId: this.vm.id,
    });
  }
}

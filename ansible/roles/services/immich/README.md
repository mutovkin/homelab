# Immich

Self-hosted photo and video management.

## Services

| Service                  | Port            | Purpose                    |
| ------------------------ | --------------- | -------------------------- |
| immich-server            | 2283            | Main API + web UI          |
| immich-machine-learning  | 3003 (internal) | Face/object recognition ML |
| immich-redis             | 6379 (internal) | Cache and job queue        |

## Dependencies

- **PostgreSQL**: Uses the shared PostgreSQL instance on this host (database: `immich`)
- **GPU** (AMD Radeon 890M): `immich-server` uses `/dev/dri` for VAAPI transcoding;
  `immich-machine-learning` runs the `-rocm` image and needs `/dev/kfd` **and**
  `/dev/dri` (#244)
- **Storage**: Upload directory on local `/data` mount

## GPU Acceleration

Immich server uses VAAPI for video transcoding and needs `/dev/dri` only.
The ML container runs Immich's `-rocm` image (ROCm + MIGraphX) and needs `/dev/kfd`
**as well as** `/dev/dri` — kfd is the compute driver, dri carries the render node.
Both are provided via Docker device mapping; the LXC host must have GPU device
passthrough configured (managed by the `proxmox_guests` role, `gpu_sharing: true`).

Deploy day, before enabling immich on a host: the `-rocm` ML image is ~35 GiB
unpacked (check the CT rootfs with `df -h /`), and MIGraphX compiles models on
first inference — minutes of 100% GPU is startup, not a hang.

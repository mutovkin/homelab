# Immich

Self-hosted photo and video management.

## Services

| Service | Port | Purpose |
|---|---|---|
| immich-server | 2283 | Main API + web UI |
| immich-machine-learning | 3003 (internal) | Face/object recognition ML |
| immich-redis | 6379 (internal) | Cache and job queue |

## Dependencies

- **PostgreSQL**: Uses the shared PostgreSQL instance on this host (database: `immich`)
- **GPU**: Uses `/dev/dri` for VAAPI hardware transcoding (AMD Radeon 890M)
- **Storage**: Upload directory on local `/data` mount

## GPU Acceleration

Immich server uses VAAPI for video transcoding, and the ML container can leverage GPU for inference.
Both require `/dev/dri` device access, provided via Docker device mapping.
The LXC host must have GPU device passthrough configured (managed by `proxmox_guests` role).

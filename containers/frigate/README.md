# Frigate

NVR (Network Video Recorder) with realtime AI object detection.

## Services

| Service | Port | Purpose |
|---|---|---|
| frigate | 5000 | Web UI |
| frigate | 8554 | RTSP feeds |
| frigate | 8555 | WebRTC |

## Dependencies

- **GPU**: Uses `/dev/dri` for VAAPI hardware video decoding (AMD Radeon 890M)
- **Storage**: NFS mount from TrueNAS for recordings (`/media/frigate`)
- **Config**: Frigate config.yml must be placed in the config directory

## Storage

Camera recordings are stored on a TrueNAS NFS share mounted into the Docker LXC.
The NFS share must be configured in TrueNAS before deploying Frigate.

Docker Compose mounts the NFS-backed directory at `/media/frigate` inside the container.

## GPU Acceleration

Frigate uses VAAPI for hardware video decoding of camera streams.
Configure `hwaccel_args` in Frigate's `config.yml`:

```yaml
ffmpeg:
  hwaccel_args: preset-vaapi
```

The LXC host must have GPU device passthrough configured (managed by `proxmox_guests` role).

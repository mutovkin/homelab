# Lyrion Music Server (LMS)

## Description

[Lyrion Music Server](https://lyrion.org/) (formerly Logitech Media Server / Squeezebox Server) is an open-source audio streaming platform. It organizes music from a local library and streams to Squeezebox-compatible players on the network using the Slimproto protocol.

Key features:

- Web-based music library management and player control
- Squeezelite / Slimproto protocol for network audio players (WiiM, Squeezebox, software players)
- Plugin ecosystem for additional streaming sources, codecs, and UI enhancements
- Multi-room synchronized playback
- Transcoding support (ffmpeg installed via custom init script)

## Network Architecture

This container uses **host networking** (`network_mode: host`). This is required for correct operation — see the rationale below.

### Why host networking

LMS uses UDP broadcast on port **3483** for player discovery. When a Squeezelite-compatible player (e.g., WiiM) scans for servers, it broadcasts to the LAN subnet asking "Are there any LMS servers here?".

Docker bridge networking (even with port mapping) does **not** forward UDP broadcast packets between subnets. A custom bridge network (e.g., `172.36.0.0/24`) isolates the container — the player's broadcast on `192.168.x.255` is silently dropped by Docker's NAT.

Host networking places the container directly on the host's network interface, so discovery broadcasts reach LMS without any NAT layer in between.

### Ports

With host networking, all ports are bound directly on the host. No `ports:` mapping is needed.

| Port  | Protocol | Purpose                                   |
| ----- | -------- | ----------------------------------------- |
| 9001  | TCP      | Web UI (configurable via `LMS_WEB_PORT`)  |
| 9090  | TCP      | CLI / JSON-RPC interface                  |
| 3483  | TCP      | Slimproto — player control channel        |
| 3483  | UDP      | Slimproto — player discovery (broadcasts) |

## Storage

| Mount point | Source                 | Purpose                     |
| ----------- | ---------------------- | --------------------------- |
| `/config`   | Host bind mount        | LMS configuration and cache |
| `/music`    | NFS volume (read-only) | Music library from TrueNAS  |

### NFS volume

The music library is served over NFS from TrueNAS via a dedicated host-only network (`vmbr2`). The NFS volume uses:

- **NFSv4** with `ro,soft` mount options
- Host-only address (`10.99.99.x`) — traffic never leaves the Proxmox host
- `timeo=100, retrans=3` — fail soft without hanging if TrueNAS is temporarily unreachable

## Data Folder Permissions

```bash
# Create the data directory
sudo mkdir -p /data/lyrion/config

# Set ownership to match container user (UID/GID 1000)
sudo chown -R 1000:1000 /data/lyrion/config
sudo chmod -R 755 /data/lyrion/config
```

## Environment Variables

| Variable            | Source                          | Purpose                                   |
| ------------------- | ------------------------------- | ----------------------------------------- |
| `HTTP_PORT`         | `lms_web_port` (default: 9001) | Web UI port                               |
| `PUID` / `PGID`    | Hardcoded 1000                  | File ownership inside container           |
| `TZ`                | `timezone` group var            | Container timezone                        |
| `EXTRA_ARGS`        | `--advertiseaddr`               | Override advertised IP for player clients |

All variables are templated by Ansible from `ansible/roles/services/lms/templates/env.j2`.

## Deployment

Deployed via Ansible:

```bash
ansible-playbook playbooks/deploy_services.yml --limit n5pro-docker
```

Or manually on the Docker host:

```bash
cd /data/lyrion
docker compose down
docker compose up -d
```

A full teardown and recreate is required when switching from bridge to host networking — a simple `restart` will not reconfigure the network.

## Player Setup (WiiM)

1. Open the WiiM app and go to **Settings > Music Services**
2. Enable **Squeezelite** output
3. The WiiM will broadcast on port 3483/UDP and discover LMS automatically
4. If the player doesn't appear in LMS, check that the WiiM and the Docker host are on the same Layer-2 subnet

# Portainer

## Description

[Portainer](https://www.portainer.io/) is a lightweight management UI for Docker environments. It provides a web-based interface to manage Docker containers, images, volumes, networks, and stacks. Portainer simplifies Docker management by offering an intuitive dashboard that allows you to monitor container status, view logs, access container terminals, and deploy applications through a user-friendly interface.

Key features:

- Web-based Docker management interface
- Container lifecycle management (start, stop, restart, remove)
- Image management and registry integration
- Volume and network management
- Stack deployment with Docker Compose
- User access control and team management
- Real-time monitoring and logging
- Terminal access to containers

## Data Folder Permissions

Portainer stores its state in a host bind mount, not a named volume:
`/data/portainer:/data`. The Ansible role creates the directory (via the shared
`services/_deploy` pipeline's `svc_data_dirs`), and Portainer runs as root inside
the container, so no extra ownership or permission setup is required.

```bash
# Inspect or back up the data on the docker host
ls -la /data/portainer
```

## Configuration

The service provides:

- Web interface on tcp/9000 — **plaintext HTTP**, not HTTPS
- Direct Docker socket access (read-write `/var/run/docker.sock`) for container
  management

Because a read-write docker socket is root-equivalent on the host, access to the
UI port is restricted by an Ansible-managed **fail-open nftables table**
(`inet portainer_fw`, see `templates/portainer-firewall.nft.j2` and
`defaults/main.yml`). Only these sources may reach tcp/9000:

- `192.168.25.20` — NPM LXC (CT 104), reverse-proxy upstream reach
- `192.168.48.0/24` — operator workstation subnet
- loopback

Everything else is dropped, including all external IPv6. The table hooks
`prerouting` at priority `-150` (before Docker's DNAT) because docker-published
ports never traverse the `input` hook. It is fail-open by design: stopping
`portainer-firewall.service` or flushing the ruleset leaves the port open rather
than the host unreachable.

The edge-agent port **8000 is not published** — no edge agents are in use.

Watchtower labels are `com.centurylinklabs.watchtower.enable=true` **and**
`com.centurylinklabs.watchtower.monitor-only=true`: Portainer is scanned and
update notifications are sent, but it is never auto-updated, since an unattended
image swap on a container holding a read-write docker socket is not acceptable.
Both labels are required — `monitor-only` alone is inert under
`WATCHTOWER_LABEL_ENABLE=true`.

Access Portainer at `http://<docker-host>:9000` from an allowlisted source after
first startup to complete the initial setup and create an admin user.

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
(`inet portainer_fw`, built by the shared `roles/nft_scoped_fw` from the per-port
allowlist `portainer_firewall.ports` — #114). That allowlist is **per-host** and lives in
`inventory/host_vars/<host>/vars.yml` (#140); the role default is `{}`, so a host
that deploys this role without declaring one fails the `nft_scoped_fw` assert
rather than silently inheriting another host's reverse-proxy IP. Allowed sources
today:

| Host | Sources allowed on tcp/9000 |
| ---- | --------------------------- |
| `eq12_docker` (192.168.25.15) | `192.168.25.20/32` — NPM LXC (CT 104), which proxies `portainer.moutovkin.com` -> `deb-docker.lan:9000`; `192.168.48.0/24` — operator workstation subnet |
| `n5pro_docker` (192.168.30.15) | `192.168.48.0/24` — operator workstation subnet only. NPM has no proxy host for this port (its only n5pro-docker upstream is `lms.moutovkin.com` -> `:9001`), so the inherited NPM grant was removed in #140 |

Loopback is always allowed on both.

Everything else is dropped, including all external IPv6. The table hooks
`prerouting` at priority `-150` (before Docker's DNAT at `dstnat`/-100): the IPv4
path to a docker-published port is DNAT'd and forwarded — it never traverses
`input` — while the `[::]` listener is served by docker-proxy via the input path;
prerouting covers both. A `fib daddr type != local accept` rule scopes the filter
to traffic addressed to this host, so container egress to some external :9000 is
unaffected. It is fail-open by design: stopping
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

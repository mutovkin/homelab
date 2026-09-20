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

## Bump log

`portainer/portainer-ee:lts` floats on the LTS channel with the `enable` +
`monitor-only` posture described above, so a new digest is reported by watchtower and
adopted only by a deliberate
`task deploy:service -- --limit eq12_docker,n5pro_docker --tags portainer`, where the
shared `_deploy` role's `pull: always` does the update. Nothing else records which
Portainer these containers actually run — this table is that record. Same convention as
`roles/services/watchtower/README.md`.

**This role is deployed on BOTH docker hosts.** Watchtower runs on each of them
independently, so a notification that arrives from one host's 04:30 session is almost
always pending on the other too. Check both and bump both in the same run, or the fleet
silently splits versions. Read the landed version from the running container's own API
(`curl -s http://localhost:9000/api/status`), not from the tag.

| Date | From → To | Notes |
| ---- | --------- | ----- |
| 2026-09-20 | **2.45.0 → 2.45.1** on both hosts (#282) | The one real version bump of the three adopted in #282 (telegraf and postgres were rebuilds at the same version). Watchtower reported `0cd22f754ac5`; that digest also carries `2.45.1`, `latest` and — currently — `sts`, so LTS and STS are the same build this round. Registry had not drifted: notified == local tag == registry at 19:25Z and again at 19:27Z immediately before the apply. **2.45.1 is a security release**, which is why it was taken promptly on a container holding a read-write docker socket: SSRF hardening on outbound requests (Helm chart resolution and Git HTTP/HTTPS now go through an SSRF-aware transport), certificate-based auth for Azure Blob backups disabled under FIPS, and **six** CVEs patched via dependency bumps that reach a server-only deployment like ours — `golang.org/x/mod` 0.40.0 (CVE-2026-56865, -56864), `golang.org/x/crypto` 0.56.0 (CVE-2026-56854, -78662, -56855) and `grpc` 1.83.2 (CVE-2026-84304) — plus Swarm networking and registry-credential fixes. The release names ten CVEs in total; the other four (kubectl v1.37.0 CVE-2026-39821, helm v4.2.4 CVE-2026-46600, Alpine openssl/libcurl/jq CVE-2026-63073 and CVE-2026-75803) are in the **kubectl shell image**, which this deployment does not use — counted separately rather than folded into one headline number. None of the known issues in the notes reach us (they are Async Edge and Podman environments; we run neither). **Verified 2026-09-20 19:27–19:28Z on both hosts:** `/api/status` reports `"Version":"2.45.1"`, and the **`InstanceID` is unchanged on each host** (`aad44a30…` on eq12_docker, `8d2c1ef2…` on n5pro_docker) — the bind-mounted `/data/portainer` state survived the recreate, which is the check that distinguishes an upgrade from a fresh install. The `inet portainer_fw` allowlist table is present on both hosts after the recreate, so the published `:9000` did not silently reopen. **Rollback — the strongest of the three adopted in #282, because this one was a real version bump:** `portainer/portainer-ee:2.45.0` still resolves upstream to `sha256:18750221de87…`, verified against the superseded local image, so it is a genuine pullable reference and satisfies `docker_host`'s prune assumption ("an immutable upstream tag whose digest was verified against the running image"). The local copy on both hosts is now dangling and unreferenced and will be reaped by the next `docker_prune` (#276/#281), which is fine precisely because the tag survives it — unlike the rebuild-class entries in the observability and postgresql logs (#284). |

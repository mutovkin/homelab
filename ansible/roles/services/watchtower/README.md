# Watchtower Container Updater

## Description

[Watchtower](https://containrrr.dev/watchtower/) is an automated Docker container update service that monitors running containers and automatically updates them when new image versions are available. It checks for updated images on a scheduled basis, gracefully stops the old container, pulls the new image, and starts the updated container with the same configuration.

Key features, as we run it:

- Scheduled scans with a per-container policy — see [Update Posture](#update-posture-83)
- Label-based selective scanning (`WATCHTOWER_LABEL_ENABLE=true`)
- Email notifications for update status
- Cleanup of old images after updates it performed
- Configurable update schedules
- Support for private registries
- Graceful container shutdown with timeout (30s)

Rolling restart is *not* in use: `WATCHTOWER_ROLLING_RESTART` is `"false"`.

## Data Folder Permissions

Watchtower does not require any persistent data directories as it operates entirely through the Docker socket. It only needs access to:

```bash
# Docker socket access (handled automatically by Docker)
# No additional permissions needed for /data directories

# The only requirement is Docker socket access:
# /var/run/docker.sock:/var/run/docker.sock

# No data folder setup required
```

Watchtower is stateless and does not persist any data to the filesystem. All configuration is handled through environment variables, and it communicates with Docker through the Docker socket.

## Configuration Notes

- Runs on schedule: 4:30 AM daily (configurable via `WATCHTOWER_SCHEDULE`)
- `WATCHTOWER_LABEL_ENABLE=true` — only containers labeled
  `com.centurylinklabs.watchtower.enable=true` are scanned at all
- Automatically cleans up old images after successful updates
- Sends email notifications about update results (Shoutrrr `smtp://` URL, assembled in
  `env.j2` from vault vars)
- Includes a 30-second timeout for graceful container shutdown

The service requires no data persistence and operates entirely through Docker API calls via the mounted Docker socket.

## Update Posture (#83)

Two labels, two independent layers. `enable=true` controls **inclusion in the scan**;
`monitor-only=true` is checked separately at the **update-action** layer. So
`monitor-only` on its own is silently inert — the container is never scanned and never
reported. Every container we care about therefore carries `enable=true`, and the policy
choice is whether it *also* carries `monitor-only=true`.

| Class | Labels | Services |
| ----- | ------ | -------- |
| Stateful / schema-migrating | `enable` + `monitor-only` | postgres, joplin-server, grafana, victoriametrics, victorialogs, nextcloud, immich-server, immich-machine-learning (lockstep with server), frigate |
| Proven breaking-change history | `enable` + `monitor-only` | vector (0.57 shipped two breaking changes and killed the log pipeline for ~30 days), telegraf (same shape: floating tag + bind-mounted config) |
| Secrets store / socket holder | `enable` + `monitor-only` | vaultwarden, portainer, telegraf, watchtower itself |
| Restart-tolerant, low blast radius | `enable` only (auto-updates) | searxng, lms, pgadmin4, immich-redis, nextcloud-redis |

Note `telegraf` qualifies twice over. It bind-mounts `/var/run/docker.sock`, and the
`:ro` flag on a unix socket is cosmetic — the Docker API is bidirectional over that
socket regardless — so it is a socket holder by the same rule that covers portainer and
watchtower.

`pgadmin4` is the one entry in the auto row that is **not** stateless: it keeps a
schema-migrating SQLite config DB holding saved database credentials. It stays on auto
deliberately. It is pinned to major `:9` so only minors arrive; its blast radius is an
admin UI rather than primary data; and a credential-holding web UI is exactly the kind of
service that benefits more from prompt security patches than from an operator gate.

Monitor-only services are **not** frozen — they are updated deliberately. The shared
`services/_deploy` role runs `pull: always`, so `task deploy:service -- --tags <svc>`
adopts the newest image for the tag, under an operator who can take a backup first.
Watchtower's role for those services is to send the email that says an update exists.

The email only works while the tag itself moves. Watchtower compares digests for the
**exact reference the container runs**, so it reports a new build of `:latest`,
`:stable`, `:release`, `:lts`, or a major line like `postgres:18` — but it can never
report a version that publishes under a *different* tag. That is the deliberate
trade-off on Watchtower's own pin below, and the reason moving any service to an
immutable tag also opts it out of notifications.

Two honest caveats on the deliberate-update story:

- **A full deploy is a fleet upgrade event.** The per-service story above assumes
  `task deploy:service -- --tags <svc>`. `task deploy:services` and `task deploy:full`
  run the same `pull: always` pipeline across *every* stack, so a single invocation
  bulk-adopts every pending monitor-only update at once — the operator gate is still
  there, but it gates the whole fleet in one step. Take backups before a full deploy,
  and prefer scoped deploys when you only mean to move one service.
- **Pending images accumulate.** Watchtower still *pulls* the candidate image for a
  monitor-only container in order to compare digests, and `WATCHTOWER_CLEANUP=true`
  only removes the old image after an update it actually performed. Superseded pulls
  become dangling and are reclaimed by a periodic `docker image prune -f`; what remains
  is at most one pending tagged image per monitor-only service, which is bounded and
  disappears the moment the update is deployed. (Suppressing the pull with a no-pull
  label was considered and rejected — it would also suppress the digest comparison,
  i.e. the notification, which is the entire point of the class.)

### Watchtower's own image is pinned

`image: nickfedor/watchtower:1.20.3` — not `:latest`. This container holds
`/var/run/docker.sock` read-write and can restart every other container on the host, and
it is a community fork of an upstream archived in 2025-12. Letting it pull and execute
its own new `latest` unattended makes the whole fleet trust an unreviewed image. Pinned,
a version bump is a compose edit in git.

`1.20.3` is an immutable release tag, so — unlike every other monitor-only service —
**Watchtower will not email us about its own new releases.** It only ever checks
`nickfedor/watchtower:1.20.3`; a published `1.20.4` is invisible to it. Its
`monitor-only` label is there to cover the one residual unattended path the pin leaves
open (a re-pushed `1.20.3`), not to provide notifications.

Checking for a new version is therefore a deliberate, manual step against
<https://github.com/nicholas-fedor/watchtower/releases>. **That URL is correct as
written** — the fork's GitHub org is `nicholas-fedor` while its Docker Hub namespace is
`nickfedor`. The two do not match, `https://github.com/nickfedor/...` 404s, and an
unclaimed namespace next to a supply-chain-sensitive image is precisely what this posture
exists to defend against. Do not "correct" it to match the image reference.

To bump: edit the tag in `files/compose.yaml`, update the digest in the comment above it,
then
`task deploy:service -- --tags watchtower` and confirm the next session still reports the
expected `scanned=N`. (Digest-pinning instead of a tag was considered and not taken: it
costs readability and `monitor-only` already prevents the unattended path. A deliberate
deploy would still adopt a re-pushed `1.20.3`.)

### Verification

Scan scope is the count, not the exit status — `failed=0` says nothing about coverage:

```bash
# containers Watchtower will scan on the next run
docker ps -q | xargs docker inspect \
  --format '{{.Name}} {{index .Config.Labels "com.centurylinklabs.watchtower.enable"}}' \
  | grep -c ' true$'

# what it actually scanned last run — the two numbers must match
docker logs watchtower 2>&1 | grep 'Update session completed' | tail -1
```

Full label matrix for a host:

```bash
docker ps --format '{{.Names}}\t{{.Label "com.centurylinklabs.watchtower.enable"}}\t{{.Label "com.centurylinklabs.watchtower.monitor-only"}}'
```

To prove scan scope **without waiting for the 04:30 session**, run a throwaway one-shot.
This is the check to use right after a label change:

```bash
docker run --rm \
  --security-opt apparmor=unconfined \
  -v /var/run/docker.sock:/var/run/docker.sock \
  nickfedor/watchtower:1.20.3 \
  --run-once --monitor-only --label-enable --no-startup-message 2>&1 \
  | grep 'Update session completed'
```

Two things make this safe and repeatable:

- `--security-opt apparmor=unconfined` is **required** on these Docker-in-LXC hosts —
  same AppArmor gotcha the compose services carry, and without it the container will not
  start.
- `--monitor-only` guarantees the probe updates nothing, whatever the labels say. It is a
  read-only count. `--rm` and the missing `enable` label on the probe itself keep it out
  of its own scan.

A monitor-only service with a pending update shows up as a notification email and **no**
container change. If a container you expected to see is missing from `scanned=N`, it is
missing the `enable` label — see
[docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md](../../../../docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md).

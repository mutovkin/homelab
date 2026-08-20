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
  disappears the moment the update is deployed. Suppressing the pull with a no-pull
  label stays **rejected**, and this is now verified rather than assumed — see below.

#### `no-pull` verified against the fork's source at v1.20.3 (#121c)

`com.centurylinklabs.watchtower.no-pull` (equivalently `--no-pull` /
`WATCHTOWER_NO_PULL`) suppresses **all registry contact** for the staleness check. It
does not fall back to a registry-side manifest/HEAD digest comparison, so a
`monitor-only` + `no-pull` container would be scanned, found "not stale", and produce
**no notification** — the exact silent-staleness failure the monitor-only class exists
to prevent. Rejected, permanently.

Verified by reading the fork's source at tag `v1.20.3` (not the docs alone):

- `pkg/container/image.go` — both staleness entry points gate on the same check.
  `IsContainerStale`: `if sourceContainer.IsNoPull(params) { return
  c.checkLocalImageStaleness(ctx, sourceContainer, clog) }` *before* `PullImage`. The
  newer registry-HEAD path `CheckContainerUpdate` carries the **identical** gate ahead
  of `digest.CompareDigestWithRemote`, so no code path reaches a remote digest
  comparison with no-pull set.
- `checkLocalImageStaleness` → `HasNewImage`, whose only lookup is
  `c.api.ImageInspect(ctx, sourceContainer.ImageName())` — the local daemon's image
  cache. With nothing else pulling on the host it can only ever return "no new image".
- Fork docs, *Update Behavior → Disable Image Pulling*: "Prevents pulling new images
  from registries, monitoring only local image cache changes… The HTTP API `/v1/check`
  endpoint also respects no-pull and inspects the local cache only."
  (<https://nicholas-fedor.github.io/watchtower/>, source at
  `docs/configuration/update-behavior/index.md` on the `v1.20.3` tag.)

So the pull traffic and the bounded image accumulation are the **price of the
notification**, not an incidental cost that a label could remove. Checked 2026-08-18
against v1.20.3, and re-checked at **v1.21.0** during that bump — `pkg/container/image.go`
still carries the `IsNoPull` gate on both entry points, ahead of
`digest.CompareDigestWithRemote`. Re-verify only if the fork changes its staleness path.

### Watchtower's own image is pinned

`image: nickfedor/watchtower:1.21.0` — not `:latest`. This container holds
`/var/run/docker.sock` read-write and can restart every other container on the host, and
it is a community fork of an upstream archived in 2025-12. Letting it pull and execute
its own new `latest` unattended makes the whole fleet trust an unreviewed image. Pinned,
a version bump is a compose edit in git.

`1.21.0` is an immutable release tag, so — unlike every other monitor-only service —
**Watchtower will not email us about its own new releases.** It only ever checks
`nickfedor/watchtower:1.21.0`; a published `1.21.1` is invisible to it. Its
`monitor-only` label is there to cover the one residual unattended path the pin leaves
open (a re-pushed `1.21.0`), not to provide notifications.

Checking for a new version is therefore a deliberate step against
<https://github.com/nicholas-fedor/watchtower/releases> — automated as a reminder by
`.github/workflows/watchtower-release-watch.yml` (#121b), which reads the pinned tag
straight out of `files/compose.yaml` every Monday and **fails** the run while it is
behind the latest upstream release. The failing check is the reminder; the bump itself
stays a human decision made with the release notes open. **That URL is correct as
written** — the fork's GitHub org is `nicholas-fedor` while its Docker Hub namespace is
`nickfedor`. The two do not match, `https://github.com/nickfedor/...` 404s, and an
unclaimed namespace next to a supply-chain-sensitive image is precisely what this posture
exists to defend against. Do not "correct" it to match the image reference.

To bump (the release-watch job above tells you when): edit the tag in
`files/compose.yaml`, update the digest in the comment above it, then
`task deploy:service -- --tags watchtower`, confirm the next session still reports the
expected `scanned=N`, **and run the delivery probe below on every host** — a bump can
break notification delivery while leaving the container healthy, and that failure is
invisible until an update you needed to hear about goes unreported (#145). Record both
results in the bump log. (Digest-pinning instead of a tag was considered and not taken: it
costs readability and `monitor-only` already prevents the unattended path. A deliberate
deploy would still adopt a re-pushed `1.21.0`.)

#### Bump log

| Date | From → To | Notes |
| ---- | --------- | ----- |
| 2026-08-19 | 1.20.3 → 1.21.0 | First bump driven by the release-watch job, which went red the day it landed. Release delta is fixes plus one behavioural change worth knowing: **logging moved from logrus to zerolog** (#2128), so log *format* changed — the `Update session completed … scanned=N` line survives it (re-verified by probe on both hosts), but anything parsing watchtower logs by shape should be re-checked. Notification field validation was tightened (#2124) and shoutrrr went to v0.17.1; our `smtp://` URL still starts clean. No change to `enable` / `monitor-only` / `WATCHTOWER_LABEL_ENABLE` semantics, so the posture model above is untouched. **Delivery probed 2026-08-20 03:24–03:25 UTC on BOTH hosts (#145)** — one real mail each, `Mail successfully sent`, probe exit 0, `scanned=12`/`scanned=3` matching the labelled counts, no live container touched. Startup-message forcing worked on both; the session-report path rendered an empty message on n5pro_docker and was skipped, which is exactly why the probe must not rely on it. |

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
  nickfedor/watchtower:1.21.0 \
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

#### Prove notification DELIVERY, not just URL validity (#145)

**Validation passing is not delivery.** The scan-scope probe above, and a healthy
container with `RestartCount 0`, prove only that the `smtp://` URL *parses* — a malformed
URL fails fast at startup. They say nothing about whether a message arrives. And the
routine scan cannot tell you either: watchtower's report-mode template renders an **empty**
message when nothing was updated or failed, and an empty message is skipped, so at
`updated=0` no send is attempted at all. A notification that never arrives is
indistinguishable from "nothing needed updating".

That matters most for the `monitor-only` class, where the email **is** the update
mechanism — there is no auto-apply to fall back on. Run this after every version bump,
on every host:

```bash
# Take the URL from the RUNNING container — that is the value watchtower actually uses.
# Exported and passed as a bare `-e NAME` pass-through, so it never enters the argv.
export WATCHTOWER_NOTIFICATION_URL=$(docker inspect watchtower \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^WATCHTOWER_NOTIFICATION_URL=//p')
[ -n "$WATCHTOWER_NOTIFICATION_URL" ] || { echo "no URL on the running container"; exit 1; }

docker run --rm \
  --security-opt apparmor=unconfined \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e WATCHTOWER_NOTIFICATION_URL \
  nickfedor/watchtower:1.21.0 \
  --run-once --monitor-only --label-enable --debug --notification-log-stdout 2>&1 \
  | sed -E 's#(smtps?)://[^[:space:]"]+#\1://REDACTED#g'
```

**How it forces a send at `updated=0`:** production sets
`WATCHTOWER_NO_STARTUP_MESSAGE=true`; the probe simply omits it, so watchtower queues its
startup notification and flushes it through shoutrrr at shutdown. That path does **not**
depend on the report template rendering anything — which is the point. Measured on
2026-08-20, the report path behaved differently on the two hosts (`msg_length=54` on
eq12_docker, `msg_length=0` and `Message empty, skipping send` on n5pro_docker) and yet
both hosts sent exactly one mail. A probe built on the report template would have proved
delivery on one host and silently proved nothing on the other.

The invariants are not optional:

- `--security-opt apparmor=unconfined` — required on these Docker-in-LXC hosts; without
  it the probe will not start.
- `--monitor-only` — the probe can never update anything, whatever the labels say.
- `--rm`, and no `watchtower.enable` label on the probe, so it is discarded and stays out
  of its own scan.
- **Redact every captured line before it is written to a file, pasted, or quoted.** The
  URL is a live mail credential and `--debug` is verbose.

**Do NOT use `--env-file /data/deploy/watchtower/.env`.** That file writes every value
single-quoted on purpose (#117 — compose dotenv interpolates `$` in unquoted values).
Compose's dotenv parser strips those quotes; **the docker CLI's `--env-file` parser does
not.** shoutrrr would receive a URL wrapped in literal `'…'`, fail to parse it, and you
would misdiagnose a quoting bug as a shoutrrr regression.

What counts as evidence, in order of strength:

1. `Mail successfully sent to "…"!` — shoutrrr's own positive confirmation. This is what
   to look for; the 1.21.0 build does print it.
2. `Notification send completed successfully … total_urls=1` and a probe exit code of 0.
3. `Update session completed … scanned=N` with N equal to the labelled count.

"No `Failed to send notification` line" is **weaker** evidence than a positive
confirmation and must never be written up as if it were the same thing.

Finally: the probe proves the **send side**. Delivery is only fully confirmed once the
mail is seen in the destination inbox — note the UTC timestamp and host of each probe so
they can be matched there. If the probe reports a send failure, this stops being a docs
question: read shoutrrr's changelog for percent-encoding / auth-field handling changes and
compare against the URL *shape* in `templates/env.j2` (never a rendered value). Changing
`env.j2` re-templates a secret-bearing file on every host and needs a deploy — and never
run `--diff` on a play that templates it.

A monitor-only service with a pending update shows up as a notification email and **no**
container change. If a container you expected to see is missing from `scanned=N`, it is
missing the `enable` label — see
[docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md](../../../../docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md).

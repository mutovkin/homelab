---
title: "Watchtower with WATCHTOWER_LABEL_ENABLE never self-updates and silently ignores monitor-only containers"
date: 2026-08-13
last_updated: 2026-08-19
category: integration-issues
module: watchtower
problem_type: integration_issue
component: tooling
symptoms:
  - "Watchtower keeps every other service current but its own image goes months out of date"
  - "A container labeled only watchtower.monitor-only=true never produces notification emails"
  - "Update session completed scanned=10 while the host runs 12 containers"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - docker-compose
  - vaultwarden
  - ansible
tags:
  - watchtower
  - docker
  - container-labels
  - auto-update
  - notifications
  - shoutrrr
  - image-pinning
  - supply-chain
  - delivery-evidence
---

# Watchtower with WATCHTOWER_LABEL_ENABLE never self-updates and silently ignores monitor-only containers

## Problem

With `WATCHTOWER_LABEL_ENABLE: "true"`, Watchtower only scans containers carrying
`com.centurylinklabs.watchtower.enable=true`. Two failure modes follow, and both are silent:

1. **Watchtower never updates itself.** Its own compose service had no such label, so it kept
   every other service current while its own image aged ~2 months (local build 2026-06-11 vs
   v1.20.3 released 2026-08-05) on both docker hosts.
2. **`monitor-only=true` alone is inert.** Vaultwarden carried *only*
   `com.centurylinklabs.watchtower.monitor-only=true`. Because that is not `enable=true`, it was
   never scanned — so it produced **no notification emails at all**, which is the exact opposite
   of what the label was added to achieve. Vaultwarden drifted ~3 months (1.36.0, image built
   2026-05-03) with no signal.

## Symptoms

- `docker logs watchtower` shows `Update session completed ... scanned=10` while `docker ps`
  lists 12 containers — the gap is the unlabeled containers.
- Watchtower's own image build date falls far behind the latest release while every other
  service is current.
- A service you believe is "monitored but not auto-updated" never generates a single email.

## What Didn't Work

- **Reading the label names at face value.** `monitor-only=true` *sounds* self-sufficient — as if
  it opts a container into monitoring. Under label-enable mode it does not; it only modifies
  behaviour for containers already in scope. The scan filter and the update action are separate
  layers, and `monitor-only` lives on the second one.
- **Trusting `failed=0` in the update session log.** Watchtower reported clean runs the entire
  time. It was doing exactly what it was configured to do; the containers simply were not in
  scope, and nothing in the log says "these were skipped".

## Solution

Add `enable=true` to Watchtower itself so it self-updates:

```yaml
# containers/watchtower/watchtower.yml
services:
  watchtower:
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
```

For a container that should be **watched but never auto-updated**, set *both* labels:

```yaml
# containers/vaultwarden/vaultwarden.yml
labels:
  - "com.centurylinklabs.watchtower.enable=true"       # brings it into scan scope
  - "com.centurylinklabs.watchtower.monitor-only=true" # prevents the actual update
```

Verify by counting: the next `scanned=N` must equal the number of containers labeled
`enable=true`.

```bash
# expected scan count for the next run
for c in $(docker ps --format '{{.Names}}'); do
  [ "$(docker inspect "$c" --format '{{index .Config.Labels "com.centurylinklabs.watchtower.enable"}}')" = true ] && echo "$c"
done | wc -l
```

## Why This Works

`enable` and `monitor-only` are enforced on **independent code paths**. `enable` controls
*inclusion in the scan* (the container filter). `monitor-only` is checked separately at the
update-action layer, which excludes the container from the restart/update list regardless of any
other label. `enable=true` therefore cannot override `monitor-only=true` — the combination is
well defined and yields "scanned, notified, never updated".

Watchtower's self-update is a supported path: it renames the old container, starts the new one,
and defers cleanup to the new instance. The one documented hard failure mode is a port collision
between the outgoing and incoming instances, which does not apply when the service publishes no
ports.

## Prevention

- **Under label-enable mode, treat `enable=true` as the ticket to the scan.** Every other
  watchtower label — `monitor-only`, lifecycle hooks, `depends-on` — only takes effect for
  containers that already hold it. A label-only-`monitor-only` container is silently invisible.
- **Assert the scan count, not the exit status.** `failed=0` says nothing about coverage. Compare
  `scanned=N` against the labeled-container count after any change to labels or to the container
  set; a drop is the signal that something fell out of scope.
- **Watchtower cannot be relied on to update itself unless explicitly labeled**, so it is the one
  container whose staleness no automation will report.
- Related gotcha from the same change: `WATCHTOWER_NOTIFICATION_URL` is parsed as a
  **space-separated list of URLs**. Any space inside the value — very common in Gmail app
  passwords — must be percent-encoded or the value shatters into garbage URLs. In Jinja,
  `| urlencode` leaves `/` untouched, so escape it explicitly:
  `{{ secret | urlencode | replace('/', '%2F') }}`. The legacy
  `WATCHTOWER_NOTIFICATION_EMAIL_*` options this replaces are deprecated and slated for removal
  in Watchtower v2.
- With `WATCHTOWER_CLEANUP=true`, images pulled for a *monitor-only* container are never "old
  images of an updated container", so they accumulate on disk until pruned separately.

## Follow-on: per-class update posture (#83)

Fixing the scan scope exposed the next question — everything in scope was on the same
posture, so the 04:30 session applied any new image unattended, including to
schema-migrating databases. #83 split the fleet into an auto class and a monitor-only
class, and pinned Watchtower itself to an immutable tag. Three durable lessons came out
of it. Full policy and the per-service table live in
`ansible/roles/services/watchtower/README.md`.

### Verify scan scope without waiting for the nightly session

The count check above is only actionable the next morning. A throwaway one-shot answers
it immediately, which is what you want right after changing labels:

```bash
docker run --rm \
  --security-opt apparmor=unconfined \
  -v /var/run/docker.sock:/var/run/docker.sock \
  nickfedor/watchtower:1.21.0 \
  --run-once --monitor-only --label-enable --no-startup-message 2>&1 \
  | grep 'Update session completed'
```

**The scan count says nothing about DELIVERY.** This probe proves coverage; it does not
prove a notification would arrive, because at `updated=0` the report template renders an
empty message and nothing is sent. Proving the send is a separate, forced probe —
see [[prove-notification-delivery-not-just-config-validity]] (#145).

`--monitor-only` makes the probe read-only regardless of what the scanned containers are
labeled, so it can never apply an update as a side effect of measuring. On Docker-in-LXC
hosts `--security-opt apparmor=unconfined` is **required** — without it the probe hits
the same AppArmor wall as any compose service here and never starts, which reads as a
tooling failure rather than the environment gotcha it is.

### Pinning a container to an immutable tag opts it out of ALL notifications

Watchtower compares digests for the **exact reference the container runs**. Its
notifications are therefore "this tag now points somewhere new", not "a newer version
exists". The distinction is invisible while everything floats on `:latest` and becomes
load-bearing the moment you pin:

- Moving tags — `:latest`, `:stable`, `:release`, `:lts`, and major lines like
  `postgres:18` or `redis:7-alpine` — keep notifying, because patch releases republish
  the same tag.
- An immutable tag like `watchtower:1.21.0` notifies about nothing but a re-push of that
  exact tag. A published `1.21.1` is simply invisible.

So `pin + monitor-only` does not mean "we will be told and can decide". It means the
unattended path is closed and **a human must check the releases page**. Pair every
immutable pin with a written manual-check step, or the pin silently becomes indefinite
staleness — the same failure this document opens with, reintroduced by the fix for it.

### Docker Hub namespace ≠ GitHub org

The fork ships as `nickfedor/watchtower` on Docker Hub but its source lives at
`https://github.com/nicholas-fedor/watchtower`. `https://github.com/nickfedor/watchtower`
**404s** — the org is unclaimed. Deriving a source URL from an image reference is a
reasonable-looking guess that lands on a squattable name adjacent to an image holding
`docker.sock` on every host, which is exactly the supply-chain shape the monitor-only
posture defends against. Record the real URL next to the pin and say why it differs, or
someone will "fix" the mismatch in the wrong direction.

## Related Issues

- #136 — the 1.20.3 → 1.21.0 bump; the probe and pin examples above track the pinned tag
- #145 — validation passing is not delivery; the scan count proves coverage, not arrival
- #71 — Watchtower never self-updates; migrate deprecated email notification config
- #72 — Vaultwarden produces no update notifications; add pre-deploy backup
- #83 — Update posture per container class; pin the update orchestrator
  (policy: `ansible/roles/services/watchtower/README.md`)
- Upstream `containrrr/watchtower` was archived 2025-12-17; this homelab runs the
  `nickfedor/watchtower` community fork.

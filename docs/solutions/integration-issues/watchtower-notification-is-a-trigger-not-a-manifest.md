---
title: "A Watchtower notification names what it staged, not what a deploy will adopt"
date: 2026-09-17
last_updated: 2026-09-20
category: integration-issues
module: containers
problem_type: integration_issue
component: tooling
symptoms:
  - "the version a deploy lands is newer than the version watchtower's mail reported"
  - "docker image inspect <repo>:latest shows an older version than a fresh pull adopts"
  - "release notes were reviewed for one version but a different version went live"
  - "two services in one deploy land a version ahead of the notification, three land exactly on it"
  - "a pre-bump review misses removals because it read the wrong release's notes"
  - "a bump log records a version change that never happened"
  - "watchtower reports a new digest but the application version is unchanged"
  - "the superseded image has no upstream tag to roll back to"
root_cause: incorrect_assumption
resolution_type: workaround
severity: medium
related_components:
  - watchtower
  - docker
  - services/_deploy
  - community.docker.docker_compose_v2
  - services/observability
tags:
  - docker
  - watchtower
  - image-updates
  - latest-tag
  - registry
  - deploy
  - verification
  - rebuild
  - rollback
---

# A Watchtower notification names what it staged, not what a deploy will adopt

## Problem

Watchtower's "Found new image" mail names an image and a digest. The obvious reading — the
one that survives right up until it costs you — is that the digest is *the update you are
about to take*, so you review that version's release notes and deploy.

It is not. The notification records the digest Watchtower resolved **at its own scan time**.
On a `monitor-only` service the adoption happens later, whenever an operator runs a
deliberate `task deploy:service`, and `_deploy`'s `pull: always` re-consults the registry at
*that* moment. Anything upstream pushed in between is what you actually get.

The trap has a second half that closes the obvious escape route. Watchtower **pulls** the
candidate image to compare digests, which moves the local `:latest` tag onto the staged
image. So the natural pre-flight check —

```bash
docker image inspect grafana/grafana:latest --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

— reports the staged version too, and **corroborates the notification**. Two independent-
looking sources agree, and both are reading the same stale local state. Measured, not
inferred — the numbers are in the next section.

## Measured (#275, 2026-09-16/17, eq12_docker)

Watchtower reported five new digests. Three landed exactly as reported; two landed a whole
release further on:

| Image | Notification said | Deploy landed |
| --- | --- | --- |
| `victoriametrics/victoria-metrics` | v1.151.0 (`6d164540a04f`) | **v1.152.0** (`86ca5fdb6d87`) |
| `grafana/grafana` | 13.2.1 (`f772d434e8fa`) | **13.2.2** (`ac461fb352ab`) |
| `telegraf` | 1.40.0 (`c25bff1bb4bf`) | 1.40.0 — as reported |
| `joplin/server` | 3.7.2 (`3f7b852959aa`) | 3.7.2 — as reported |
| `vaultwarden/server` | 1.37.3 (`1587c45feaa4`) | 1.37.3 — as reported |

**The local-tag corroboration was measured too, before the deploy.** At 06:17Z, roughly
five minutes before the apply, `docker image inspect <repo>:latest` on the host reported:

All digests below are manifest(-list) digests, truncated.

| Image | Local `:latest` resolved to | Registry held |
| --- | --- | --- |
| `victoria-metrics` | `6d164540a04f` — **v1.151.0**, the notified digest | `86ca5fdb6d87` (v1.152.0) |
| `grafana` | `f772d434e8fa` — **13.2.1**, the notified digest | `ac461fb352ab` (13.2.2) |

So the local tag agreed with the mail, digest for digest, while both disagreed with the
registry. That is the whole trap in one line: the check you would reach for to
double-check the notification is reading the artefact the notification created.

Note this evidence is **not** reproducible after the fact — the deploy's `pull: always`
moved those local tags — so it has to be captured before the apply or not at all.

The registry timestamps explain it exactly, and are the thing to query when the versions
disagree:

```bash
curl -s https://hub.docker.com/v2/repositories/grafana/grafana/tags/latest \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['tag_last_pushed'], d['digest'])"
# 2026-09-15T09:19:32Z  sha256:ac461fb352abc50da...
```

`grafana:latest` was repushed 2026-09-15 and `victoria-metrics:latest` 2026-09-14 — both
*before* the deploy, both *after* the scan that produced the mail. The three that matched
simply had not been repushed in that window. So the failure is silent and intermittent: most
images agree most of the time, which is precisely what makes the assumption survive.

## Why it matters more than a version-number nit

The pre-bump procedure this repo mandates is to read the target release's notes for removals
and renames, then grep the whole alert and dashboard corpus for every affected name
(`removed-metric-did-not-go-nodata-mixed-version-fleet.md`). That procedure is only as good
as the version you point it at. Reviewing v1.151.0 and deploying v1.152.0 means the grep ran
against the wrong changelog — and on this fleet a removed metric does **not** reliably
announce itself as NoData.

In #275 the extra deltas happened to be benign (no `vm_*` removals, no MetricsQL changes, and
Grafana 13.2.2 was pure security/bugfix). That is luck, not a control.

## The sibling error: a moved digest is not a version bump (#282, 2026-09-20)

The failure above is "the digest moved further than the mail said". Its mirror image is
"the digest moved and the **version did not move at all**" — and it produces a bump log
that records a change which never happened.

Official images are rebuilt under their existing version tags whenever a base layer is
patched. The floating tag moves, watchtower reports a new digest, and nothing about the
application changed. Measured on eq12_docker, adopting three reported images:

| Image | Notified digest | Also tagged | Running before | Landed |
| ----- | --------------- | ----------- | -------------- | ------ |
| `telegraf:latest` | `777cdbf33325` | **`1.40.0`**, `1.40` | 1.40.0 | **1.40.0** |
| `postgres:18` | `86c951e05bf5` | **`18.6`**, `18.6-trixie`, `18-trixie`, `trixie` | 18.6 (`18.6-1.pgdg13+2`) | **18.6** (`18.6-1.pgdg13+2`) |
| `portainer/portainer-ee:lts` | `0cd22f754ac5` | **`2.45.1`**, `sts` | 2.45.0 | **2.45.1** |

Two of the three "updates" were rebuilds at the version already running — identical for
postgres down to the pgdg package revision, read out of the server both before and after.
Only Portainer was a real release with notes to review.

This matters in both directions. A row reading `1.39.x → 1.40.0` would have been fiction,
and the log is the only memory a `:latest` fleet has. In the other direction, a rebuild is
**still worth adopting** — base-layer CVE patching is the entire value — so "no version
change" is not a reason to skip the deploy, only a reason not to invent release notes for
it. It also tells you what to verify: with no application delta, the risk is the runtime
environment, which is exactly where the setuid/fscaps trap lives: a rebuilt image ships a
new `/usr/bin/ping`, so re-check `getcap` **and the plugin's own output** — the
`no_new_privs` carve-out in CLAUDE.md's Docker-in-LXC gotcha (#95), where telegraf stayed
healthy while eight ping metrics silently went to NO DATA.

**Resolve the digest to a version before writing the row.** Map it back through the
repository's tag list — note the `library/` namespace for official images, and
`page_size=100`, because the matching tags are spread across a paginated default of 10:

```bash
# which version tags share the digest watchtower reported?
repo=library/postgres            # portainer/portainer-ee for a non-official image
want=sha256:86c951e05bf56c93d95d397747fb8820ac76cc3bedb78f43abd83eedbe3666ae
for p in 1 2 3; do
  curl -s "https://hub.docker.com/v2/repositories/${repo}/tags?page_size=100&page=${p}" \
  | python3 -c 'import json,sys
want=sys.argv[1]
for r in json.load(sys.stdin).get("results",[]):
    if r.get("digest")==want: print(r["name"], r.get("tag_last_pushed"))' "$want"
done
```

If the answer includes a concrete version tag equal to what is already running, the entry
is a rebuild. Say so in the log, and say what you verified instead of release notes.

### A rebuild also costs you the rollback, quietly

`roles/docker_host` prunes dangling images (#276/#281) and justifies it on the grounds that
"every service's rollback path is an immutable upstream tag whose digest was verified
against the running image". **A rebuild breaks that guarantee**, because the version tag you
would roll back to now points at the image you just adopted:

| Superseded image | Pullable rollback tag after the bump? |
| ---------------- | ------------------------------------- |
| `portainer-ee` `18750221de87` | **yes** — `portainer/portainer-ee:2.45.0` still resolves to it (real version bump) |
| `telegraf` `c25bff1bb4bf` | **no** — `telegraf:1.40.0` is now the new build; `1.39.3` is a downgrade |
| `postgres` `4ef4dbc939d6` | **no** — `postgres:18.6` is now the new build, and no `18.5` tag exists |

Measured after #282's apply: all three report `containers=0`, `RepoTags=[]`,
`RepoDigests=[]` — plain dangling images the next `docker_prune` deletes, after which the
rebuild-class ones are unrecoverable. So for a rebuild, record honestly that the image-side
rollback is local-only and time-limited, and lean on the data-side fallback where one exists
(the verified `pg_dumpall` for postgres). Tracked as #284.

## Fix

**Do not treat the notification as a manifest. Treat it as a trigger.**

1. Before the deploy, resolve what `:latest` *currently* points at from the **registry**, not
   from the local tag:
   ```bash
   curl -s https://hub.docker.com/v2/repositories/<ns>/<repo>/tags/latest \
     | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['tag_last_pushed'], d['digest'])"
   ```
   Compare that digest to the running container's image id, and review the notes for the
   range between them — which may be more than one release.

   **That comparison works on this fleet, and the reason is worth knowing before you
   copy it elsewhere.** Docker Hub's `digest` is the manifest-LIST digest; a classic
   overlay2 docker reports a container's `.Image` as the config-blob digest, and those
   two never match. This host runs the **containerd image store** (`docker info` →
   `driver=overlayfs`, with the real bulk under `/var/lib/containerd`), which records
   the image by its manifest digest instead — so they are equal. Measured on stable
   pinned tags: `postgres:18` Hub list digest `86c951e05bf5…` = local `.Id`
   `86c951e05bf5…`; `dpage/pgadmin4:9` `c332c5f6dfba…` = `c332c5f6dfba…`; likewise all
   five images in the table above. The portable form, correct on either storage
   backend, is `docker image inspect <repo>:<tag> --format '{{index .RepoDigests 0}}'`.

   A worked non-match: `searxng:latest` resolved locally to `0e8d3a8df66b…` against a
   Hub digest of `547fdc19b455…`. That is not a digest-type mismatch — searxng is
   `enable`-only and auto-updates, so its tag had simply moved on again. A mismatch
   here means "the tag moved", which is the signal you are looking for.
2. After the deploy, read the landed version out of the **running container** and record it:
   ```bash
   docker exec grafana grafana server -v
   docker inspect <c> --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
   ```
   If it is not what you reviewed, review the difference **before** calling the deploy done.
3. Write the landed version into the role's bump log. A `:latest` fleet has no memory: the
   running version exists only on the host, so an unrecorded bump is unauditable the moment
   the container is replaced. See the bump-log sections in
   `roles/services/{observability,joplin,vaultwarden,postgresql,portainer}/README.md` and the
   pinned-tag variant in `roles/services/watchtower/README.md`.

## What does not work

- **Inspecting the local `:latest` tag.** It shows Watchtower's staged image. This is the
  specific check that looks like independent confirmation and is not.
- **Pinning to the notified digest to "lock in" what you reviewed.** Pinning any image to an
  immutable reference opts it out of *all* Watchtower notifications, because Watchtower only
  checks the reference the container runs
  (`watchtower-label-enable-scan-scope.md`). You would trade a version surprise for silence.
- **Deploying immediately on the mail to narrow the window.** It shrinks the gap without
  closing it, and it trades away the deliberate-review property that `monitor-only` exists to
  buy.

## Related

- `docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md` — scan scope,
  posture classes, and why pinning silences notifications.
- `docs/solutions/integration-issues/removed-metric-did-not-go-nodata-mixed-version-fleet.md`
  — why "review the target release's notes" is load-bearing on a version-skewed fleet.
- `docs/solutions/integration-issues/compose-up-recreates-watchtower-created-containers.md`
  — the other way Watchtower's pulls surface in a later deploy.

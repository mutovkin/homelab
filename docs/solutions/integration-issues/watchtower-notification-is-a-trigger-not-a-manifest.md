---
title: "A Watchtower notification names what it staged, not what a deploy will adopt"
date: 2026-09-17
last_updated: 2026-10-08
category: integration-issues
module: containers
problem_type: integration_issue
component: tooling
symptoms:
  - "the version a deploy lands is newer than the version watchtower's mail reported"
  - "docker image inspect <repo>:latest corroborates the notification while the registry has already moved on"
  - "watchtower reports a new digest but the application version is unchanged"
  - "watchtower reports a new digest but the linux/amd64 manifest and config digests are identical to the running image"
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
  - services/postgresql
tags:
  - docker
  - watchtower
  - image-updates
  - registry
  - rebuild
  - rollback
  - containerd
  - multi-arch
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

**It recurred in #296 (2026-10-08, eq12_docker), on two of seven adopted images.** This time
the further-on image was a rebuild at the same version, not a newer release:

| Image | Notification said | Deploy landed |
| --- | --- | --- |
| `telegraf:latest` | 1.40.1, built 2026-09-21 (`90da3a5b8142`) | **1.40.1, built 2026-10-06** (`e91237482edc`; amd64 `09c3f2a6e398`, config `b06095d88197`) |
| `postgres:18` | `5a5a84b19854`, not a new image at all (see [below](#a-new-index-digest-is-not-a-new-image-296-2026-10-08)) | **18.6, built 2026-10-06** (`74935e722416`; amd64 config `29754c7520f4`) |

The other five (VictoriaMetrics, VictoriaLogs, vector, grafana, vaultwarden) landed as notified.
Registry digests were resolved at 03:24Z and again at 03:32Z, immediately before the apply, with
no drift between the two checks. Same-version drift still matters: the runtime checks for a
rebuild (`getcap /usr/bin/ping`, the metric-name set) have to run against the image that
LANDED, built 10-06, not the 09-21 build the mail named.

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

### What a rebuild actually requires you to verify

With no application delta there are no release notes to grep, so the verification moves
from the changelog to the runtime. Two checks generalise, and both are cheap:

- **The metric-name set, before vs after.** One query covers every plugin at once and is the
  direct answer to #216 (a name that disappears from an unlabelled aggregate does *not* go
  NoData). Measured across #282's telegraf recreate: 259 names before, 259 after, none lost.

  ```bash
  curl -sG -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
    --data-urlencode 'match[]={host="eq12_docker"}' \
    --data-urlencode 'start=...' --data-urlencode 'end=...' \
    http://localhost:8428/api/v1/label/__name__/values
  ```

- **For a database, the collation version** — the failure that object counts are structurally
  blind to. A rebuild moves base OS packages, and a glibc bump can change collation ordering
  and silently invalidate every text index; `pg_isready`, the healthcheck, client
  connectivity and identical table/index counts all stay green over it. The query (and the
  `IS NOT NULL` that stops it reporting `template0` on a healthy cluster) is in
  `roles/services/postgresql/README.md`'s pre-bump list.

And one trap that is not about rebuilds at all but shares the deploy: **a real version bump
can migrate state one way, so an image rollback is not a state rollback.** Portainer 2.45.1
migrated its BoltDB and refreshed RBAC roles and user authorizations on both hosts during
#282; reverting the image alone would leave the 2.45.0 binary facing a 2.45.1 database. It
wrote its own single-slot `portainer.db.bak` first, which the next upgrade overwrites.
`docker logs <svc> | grep -i migrat` before calling a bump verified.

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
rebuild-class ones have no TAG left to pull. They are not necessarily gone: measured in #296
(2026-10-08), `library/postgres@sha256:4ef4dbc939d6…`, `library/telegraf@sha256:c25bff1bb4bf…`
and `library/postgres@sha256:86c951e05bf5…` all still served their manifest and config by
digest — but upstream makes no promise to keep an untagged index, so treat by-digest pulls as
best-effort. So for a rebuild, record honestly that the image-side rollback is untagged and
time-limited, and lean on the data-side fallback where one exists
(the verified `pg_dumpall` for postgres). Tracked as #284.

## A new index digest is not a new image (#296, 2026-10-08)

The two errors above form a chain: "the tag moved" does not mean "the version moved". #296
measured one more step down: **the tag moved, but nothing for our platform moved at all.**

Every digest the #275 and #282 sections quote (watchtower's, Docker Hub's `digest`, the local
`.Id` on the containerd store) is the multi-arch **index** (manifest-list) digest. The index lists
one manifest per platform, and its digest changes whenever any entry or annotation changes,
including entries for platforms we never pull. Measured on eq12_docker, from the registry API
and `docker image inspect`:

| | Index digest | linux/amd64 manifest | config | `Created` | `PG_VERSION` |
| --- | --- | --- | --- | --- | --- |
| `postgres:18` running | `86c951e05bf5` | `0377e72c5289` | `662db3da228c` | 2026-09-19T00:36:15.60117931Z | `18.6-1.pgdg13+2` |
| `postgres:18` notified | **`5a5a84b19854`** | `0377e72c5289` | `662db3da228c` | 2026-09-19T00:36:15.60117931Z | `18.6-1.pgdg13+2` |
| `postgres:18` at deploy (landed) | `74935e722416` | `885953109528` | **`29754c7520f4`** | 2026-10-06T01:33Z | `18.6-1.pgdg13+2` |

Both indexes have 16 entries (8 platforms, each with its attestation manifest), and diffed
entry by entry they differ ONLY in `linux/riscv64` (`7a77e12ff4d5` → `8e8f50c979fc`) and that
platform's attestation; no top-level annotation moved. The notified "update" was an index-only
repush: a different index digest over the byte-identical amd64 image. The digest→tag lookup in the #282 section
would have classified it as a rebuild at 18.6. It was not even that. By the deploy the registry
had moved again, to a real 2026-10-06 base-layer rebuild, and that is what landed.

So there are three levels. A level-1 change alone changes nothing that runs; level 2 does even
without level 3 (that is a rebuild — the landed postgres and telegraf here); level 3 is a
version bump:

| Level | What moved | How to tell |
| --- | --- | --- |
| 1. Index | the tag's index digest | Hub `digest`, watchtower's mail, local `.Id` |
| 2. Platform image | linux/amd64 manifest + config digest | the registry API recipe below |
| 3. Application | the version inside the image | digest→tag lookup (#282 section), then the running binary |

**Why the false level-1 positive is not free.** The containerd store records the container's
`.Image` as the index digest, so a deploy that adopted `5a5a84b19854` would have looked to
compose like a new image and recreated postgres. That is an inference from the measured digests
(the registry moved before it could be tested): a client-visible restart of the primary database,
plus the `pg_dumpall` the compose file mandates before any postgres bump
(`roles/services/postgresql/files/compose.yaml`, #83), for zero bytes of change on amd64.

**The rollback check has the same blind spot, in the opposite direction.** After #296,
`timberio/vector:0.58.0-distroless-static` resolved upstream to index `f41132f36751`, while the
previously running image was `385a8e948b32`. Both carry amd64 manifest `3e60640c2a00` and config
`0f797d9892bf`, so it is the same image. (A different shape from postgres: here no platform image
was repushed at all — only the attestation manifests changed.) A check of "does the rollback tag's digest equal the
running image?" done at the index level fails here, and reports a valid rollback tag as missing.
`docker_host`'s prune comment depends on exactly this check ("an immutable upstream tag whose
digest was verified against the running image"), so it must be done at level 2.

**Recipe: compare at level 2.** This uses no `jq` and nothing beyond python3's stdlib, so it runs
on both operator platforms. It works for official images (`library/<repo>`) and namespaced ones,
and for refs that are tags or FULL digests (a truncated digest is an HTTP 400). It expects an
index: on a single-arch ref there is no `manifests[]` and the script dies with `IndexError` at
`amd[0]`.

```python
# manifests.py: print index, linux/amd64 manifest, and config digest per ref
import json, sys, urllib.request
repo = sys.argv[1]
tok = json.load(urllib.request.urlopen(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"))["token"]
ACC = ",".join(["application/vnd.oci.image.index.v1+json","application/vnd.docker.distribution.manifest.list.v2+json","application/vnd.oci.image.manifest.v1+json","application/vnd.docker.distribution.manifest.v2+json"])
def get(ref):
    r = urllib.request.Request(f"https://registry-1.docker.io/v2/{repo}/manifests/{ref}", headers={"Authorization": "Bearer "+tok, "Accept": ACC})
    with urllib.request.urlopen(r) as resp: return resp.headers.get("Docker-Content-Digest"), json.load(resp)
for ref in sys.argv[2:]:
    d, idx = get(ref)
    amd = [m for m in idx.get("manifests", []) if m.get("platform", {}).get("architecture") == "amd64" and m["platform"].get("os") == "linux"]
    _, man = get(amd[0]["digest"])
    print(f"{ref:12} list={d[7:19]} amd64={amd[0]['digest'][7:19]} config={man['config']['digest'][7:19]}")
```

```bash
running=$(ssh root@<host> "docker inspect -f '{{.Image}}' postgres")   # full index digest — on the containerd image store only
python3 -I manifests.py library/postgres "$running" sha256:<notified-full-digest> 18
# overlay2 instead: .Image is the CONFIG digest, which the registry cannot resolve. Take the
# index digest from the image's RepoDigests (outer single quotes: the $(...) runs on the host).
# A dangling image has RepoDigests=[] and this fails with "index out of range" -> empty $running.
#   running=$(ssh root@<host> 'docker image inspect -f "{{index .RepoDigests 0}}" "$(docker inspect -f "{{.Image}}" postgres)"' | cut -d@ -f2)
```

Read the output this way. Same `config` means the same image: skip the bump (and the
recreate). Different `config` means a real image change: map it to a version and run the
rebuild or version-bump checks above. The same command with the rollback tag in place of
`18` verifies a rollback path.

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
   the image by its index (manifest-list) digest instead — so they are equal. Measured on stable
   pinned tags: `postgres:18` Hub list digest `86c951e05bf5…` = local `.Id`
   `86c951e05bf5…`; `dpage/pgadmin4:9` `c332c5f6dfba…` = `c332c5f6dfba…`; likewise all
   five images in the table above. The portable form, correct on either storage
   backend, is `docker image inspect <repo>:<tag> --format '{{index .RepoDigests 0}}'`.

   **Equal proves identity, but unequal does not prove a difference.** Every digest in this
   comparison, `RepoDigests` included, is an index digest, and an index can be repushed over a
   byte-identical amd64 image (measured #296: `postgres:18` `5a5a84b19854` vs running
   `86c951e05bf5`, same amd64 config `662db3da228c`). On a mismatch, run the level-2 recipe in
   [A new index digest is not a new image](#a-new-index-digest-is-not-a-new-image-296-2026-10-08)
   before concluding anything changed.

   A worked non-match: `searxng:latest` resolved locally to `0e8d3a8df66b…` against a
   Hub digest of `547fdc19b455…`. That is not a digest-type mismatch — searxng is
   `enable`-only and auto-updates, so its tag had simply moved on again. A mismatch
   here means "the tag moved" — the signal to look closer, but not yet proof that
   the amd64 image moved (see the paragraph above).
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
- **Comparing index digests to decide "same image or not".** It is right only when they are
  equal. An index-only repush gives a new index digest over an unchanged amd64 image, which in
  #296 made a no-op look like a postgres update and made a valid vector rollback tag look like
  a different image. Compare the linux/amd64 config digest.
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

---
title: "Extend a container via its image's init hook, idempotently — not via a per-start install"
date: 2026-06-20
category: conventions
module: containers
problem_type: convention
component: tooling
severity: low
applies_when:
  - "a container needs a package the base image lacks (e.g. ffmpeg in Lyrion)"
  - "an image provides an init/custom hook directory (e.g. LMS /config/custom-init.sh)"
  - "writing a script that runs on every container start"
related_components:
  - docker-compose
  - lms
tags:
  - docker
  - lms
  - container-extension
  - idempotency
  - init-hook
---

# Extend a container via its image's init hook, idempotently

## Context

Lyrion (LMS) needs `ffmpeg` for transcoding, which isn't in the base image. The
LMS image runs `/config/custom-init.sh` on every start — its sanctioned
extension hook. The original script ran `apt-get update && apt-get install ffmpeg`
**unconditionally on every container start**: slow, network-dependent, and noisy,
and it reads like an Ansible-mutates-a-running-container boundary leak.

## Guidance

Using the image's own init-hook is the right layer (it's image-provided, not
Ansible reaching into a live container). But make the script **idempotent** so a
normal restart is a fast no-op:

```bash
#!/bin/bash
# Managed by Ansible — installs ffmpeg once; no-op on subsequent starts.
if ! command -v ffmpeg >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install --no-install-recommends -qy ffmpeg
fi
```

Prefer an image that already bundles the dependency, or a small derived
Dockerfile, when the install is heavy or the package set grows. For a single
package in a homelab, a guarded init-hook script is the pragmatic choice — just
guard it.

## Why This Matters

An unguarded per-start install adds latency and a network dependency to every
restart and can fail transiently (mirror down) — turning a routine restart into a
flaky one. Guarding makes restarts deterministic and fast.

## When to Apply

Any time you extend a container through an init hook or entrypoint wrapper: gate
the mutation on "is it already done?" so the hook is idempotent across restarts.

## Examples

- Before: `apt-get update && apt-get install ffmpeg` every start.
- After: the `command -v ffmpeg` guard above (issue #12).

## A verification caveat learned here

Live end-to-end verification of this fix was blocked by an **unrelated** external
dependency: the lms stack could not start because its `/music` NFS volume from
TrueNAS (10.99.99.2) was down (`clnt_create: RPC: Unable to receive`), a
pre-existing outage (filed separately). The init-hook change itself deployed
correctly (script present and guarded on disk). Lesson for the change loop: when
the final "service healthy" check is blocked by an external dependency, verify
what you *can* (artifact deployed correctly), **file the external issue**, and be
explicit that end-to-end health was not confirmed — don't claim a green you
didn't observe. See [[ansible-change-loop-pitfalls]].

## Related

- Issue #12 (this change); issue for the TrueNAS NFS outage that blocked verification.
- [[docker-compose-shared-network-subnet-recreate]] — another Compose deploy gotcha.

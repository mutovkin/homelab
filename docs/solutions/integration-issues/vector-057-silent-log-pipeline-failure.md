---
title: "Vector 0.57 broke the log pipeline twice: a loud crash loop and a silent 401"
date: 2026-08-13
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "vector container Restarting with ExitCode 78 and a huge RestartCount"
  - "Configuration error: template references event fields but has no literal string prefix"
  - "Vector runs cleanly but VictoriaLogs returns 401 and every batch is dropped"
  - "docker compose up fails with dependency failed to start container victorialogs is unhealthy"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - vector
  - victorialogs
  - grafana
  - watchtower
tags:
  - vector
  - victorialogs
  - observability
  - breaking-change
  - healthcheck
  - scratch-image
  - silent-failure
---

# Vector 0.57 broke the log pipeline twice: a loud crash loop and a silent 401

## Problem

An unattended Watchtower update moved `timberio/vector` to 0.57.0, which shipped **two**
independent breaking changes. Docker log collection into VictoriaLogs was dead for ~30 days
(`RestartCount=42684`) and nothing reported it. Fixing the obvious failure would have left the
pipeline just as broken, only quietly.

## Symptoms

- `vector` in `Restarting (78)`, logging:
  `Configuration error. error=Sink "victorialogs": template references event fields (["hostname"])
  but has no literal string prefix to derive a confinement base from.`
- After fixing that: Vector starts clean, `RestartCount=0` — but logs
  `Server responded with an error: 401 Unauthorized` and
  `component_events_dropped ... count=285` on a loop. **VictoriaLogs' newest entry stays frozen at
  the date Vector originally broke.**
- Any redeploy of the stack fails with
  `dependency failed to start: container victorialogs is unhealthy`.

## What Didn't Work

- **Stopping at "the container is Up."** After fixing the crash loop, every surface-level check
  passed: container running, restart count zero, no config error. The pipeline was still dropping
  100% of events. Only querying VictoriaLogs for recent entries revealed it.
- **Assuming a credential mismatch.** The 401 looked like drift between Vector's and VictoriaLogs'
  credentials. Comparing them by hash proved they were *identical* — the first comparison appeared
  to mismatch only because the extraction picked up a trailing JSON bracket. The credentials were
  never the problem.
- **Editing the repo's `vector.yaml` and redeploying.** The compose file bind-mounts
  `/data/vector/vector.yaml`, but the Ansible role only created the *directory* — nothing deployed
  the file. The repo copy and the live file had to be reconciled by hand, which is how the config
  drifted unnoticed in the first place.

## Solution

**1. Template confinement** — opt out for that sink only, preserving existing label values:

```yaml
sinks:
  victorialogs:
    type: loki
    dangerously_allow_unconfined_template_resolution: true
    labels:
      container_name: "{{ container_name }}"
```

A static prefix (`docker-{{ container_name }}`) is the other option, but it rewrites every stored
label value and breaks existing Grafana queries.

**2. Environment variable interpolation** — 0.57 disabled `${VAR}` in config files by default:

```yaml
# compose, vector service
environment:
  VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION: "true"
```

Without it Vector sends the auth block **literally** as `${VL_AUTH_USERNAME}`. Note that compose
interpolates `${VAR}` inside the *compose file*, so sibling services reading credentials from
their `command:` were unaffected — only the bind-mounted config Vector parses itself broke.

**3. The impossible healthcheck** — `victoriametrics/victoria-logs` is built `FROM scratch`: no
shell, no wget (`stat /bin/sh: no such file or directory`). Any exec-based probe fails by
construction. Remove it and depend with `condition: service_started`. Note its sibling
`victoria-metrics` *does* ship wget, so keeping that healthcheck is correct — the asymmetry is
real, not an oversight.

**4. Deploy the config from Ansible**, so a repo edit actually reaches the container, and restart
only the services whose config changed.

## Why This Works

The two Vector changes fail at different layers. Template confinement is a *startup* check — loud,
crash-looping, impossible to miss. Env-var interpolation is a *runtime* behaviour — Vector starts
happily and only the receiving end rejects the request. A fix verified by "does it start?" passes
while the pipeline delivers nothing.

The healthcheck problem is structural: `depends_on: condition: service_healthy` against a service
that *cannot* become healthy is a permanent deploy block. It stayed hidden because the containers
already existed; it only fires when something forces a recreate — which is exactly when you are
trying to fix something else.

## Prevention

- **Assert the data arrived, not that the process is running.** For any pipeline, the check is a
  query against the destination for recent records — `_time:5m` returning rows — never container
  state. "Up" and `RestartCount=0` are consistent with dropping 100% of events.
- **Read the whole upgrade guide, not the entry matching your error.** One breaking change was
  visible in the logs; the second was in the same release notes and would have taken another
  month to notice.
- **Never `depends_on: service_healthy` a scratch/distroless image.** Docker has no built-in
  TCP/HTTP probe — a healthcheck needs a binary *inside* the image. If there is no shell, there is
  no healthcheck, and the dependency must be `service_started`.
- **If a compose file bind-mounts a config path, Ansible must own that path.** A bind mount the
  role only `mkdir`s is a silent-drift generator: the repo becomes decorative and the live file is
  whatever was last copied by hand. Compose does not restart on bind-mount content changes either,
  so deploying the file must be paired with an explicit restart of the affected service.
- Floating tags plus unattended updates put breaking changes into infrastructure services with no
  human in the loop. This outage is what that costs; the notification path is the only thing that
  makes it survivable.

## Related Issues

- #73 — this fix
- [[watchtower-label-enable-scan-scope]] — why the update that broke this arrived unannounced
- [[ansible-change-loop-pitfalls]] — the "a skipped branch is an untested branch" entry is the
  same verification failure in a different form

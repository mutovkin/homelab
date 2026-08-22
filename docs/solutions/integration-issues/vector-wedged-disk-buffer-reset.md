---
title: "Vector can wedge on its own disk buffer while the container stays Up"
date: 2026-08-18
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "No new logs in VictoriaLogs / Grafana while the vector container is Up and every deploy is green"
  - "docker logs vector repeats buffer or checkpoint errors and never recovers"
  - "Log volume drops to zero at an exact timestamp and never resumes"
  - "Restarting the vector container changes nothing"
root_cause: state_corruption
resolution_type: runbook
severity: high
related_components:
  - vector
  - victorialogs
  - grafana
tags:
  - vector
  - observability
  - disk-buffer
  - runbook
---

# Resetting a wedged Vector disk buffer

## The failure mode

Vector's sink buffer and its file-source checkpoints live on disk at
`/data/vector/data` (mounted at `/var/lib/vector`), deliberately outside the
container so they survive recreation (#82). That persistence is also the trap: a
buffer that fills because the sink was unreachable for a long window, or whose
data files are damaged, is **restored** on every restart. Vector comes back up,
reports itself healthy, keeps the container `Up`, and ships nothing.

Nothing else in the stack notices. Compose is happy, the healthcheck (there isn't
one on vector) proves nothing, and the deploy is green. This has already happened
in a different guise — see
[vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md),
where the pipeline was dead for ~30 days.

## Confirm it before you wipe anything

A wedged buffer is one of several causes of "no logs". Rule the cheap ones out
first — read-only, from the operator machine:

```bash
ssh root@192.168.25.15 'docker ps --filter name=vector --format "{{.Status}}"'
ssh root@192.168.25.15 'docker logs --tail 100 vector'
ssh root@192.168.25.15 'du -sh /data/vector/data'
```

- **Container restarting / ExitCode 78** → config error, not the buffer. Fix the
  config and re-deploy.
- **401 from VictoriaLogs in the logs** → credentials or interpolation, not the
  buffer. That is the #057 failure; the buffer will drain once auth works.
- **VictoriaLogs itself down / unreachable** → fix that first. Vector is supposed
  to buffer through it; wiping the buffer here throws away good data.
- **Buffer errors, a buffer at its size cap that never drains, or a corrupt-data
  message that repeats across restarts** → this runbook.

Confirm the destination is actually idle, not just quiet, with the ingest-recency
alert (`VictoriaLogs ingest stalled`, provisioned by this role) or directly:

```bash
# from eq12_docker; credentials come from the vault, do not paste them into history
curl -s -u "$VL_USER:$VL_PASS" \
  'http://127.0.0.1:9428/select/logsql/query' --data-urlencode 'query=_time:5m | count()'
```

## The fix

An Ansible task, not an SSH session (Critical Rule 1). It is tagged `never`, so it
runs only when asked for by name:

```bash
cd ansible
export ANSIBLE_VAULT_PASSWORD_FILE=../.vault_password
ansible-playbook playbooks/deploy-services.yml \
  --limit eq12_docker --tags vector_buffer_reset
```

What it does, in `roles/services/observability/tasks/vector-buffer-reset.yml`:

1. asserts `/data/vector/data` exists and is a directory,
2. **stops** the vector container — deleting underneath a running process leaves it
   writing to unlinked inodes and recreating the exact state you are clearing,
3. deletes the **contents** of `/data/vector/data` and never the directory itself
   (it is a bind-mount target; removing it points the mount at a deleted inode, and
   Ansible would recreate it root-owned with a fresh mode — the "never chown
   /data/vector" footgun of #109),
4. starts vector again,
5. re-inspects the container and **asserts it is Running** — `docker_container`
   returns on issuance, not on service, so a container that starts and immediately
   crash-loops would otherwise leave the task green.

## What it costs

- Everything still queued in the buffer is **lost**.
- The file-source checkpoints go with it. Since #143 that cost is **duplication
  rather than loss** — it used to be silent loss — but the two source groups are
  bounded very differently, and the difference matters:
  - **Host logs**: one file, `/var/log/structured.log`, read with
    `read_from: beginning` and no `ignore_older_secs`, rotated weekly with
    `maxsize 100M`. A reset re-reads at most one rotation period.
  - **Package-audit logs**: NOT the same bound, despite an earlier version of this
    note saying so. Debian rotates `dpkg.log` and `apt/history.log` **monthly,
    `rotate 12`**, and the `unattended-upgrades` logs **monthly, `rotate 6`** — so
    a reset can re-read up to **a year** of package history, not a week. The
    ~293-line figure is today's volume, not a bound.

    **Corrected by #154 — the second half of this used to be worse than it now
    is.** Until #154, every re-read record was also re-stamped with *ingest* time
    (nothing parsed these lines' own timestamps), so each reset both multiplied the
    audit trail *and dragged its apparent dates forward*. `parse_pkg` now parses
    the in-line timestamps, so a re-read line keeps the time it actually happened.
    A reset after #154 therefore costs **duplication only, not re-dating** — the
    chronology survives. Two residual effects worth knowing:
    - a re-read line older than VictoriaLogs' **90-day retention** is dropped on
      ingest rather than re-dated, so the audit trail's depth is the retention
      depth, not the log files' depth;
    - lines that genuinely carry no timestamp (apt history block bodies, dpkg
      progress spam) still take ingest time, and since #154 they say so —
      `timestamp_source:"ingest"`, which before #154 no record could ever report.
      Query `source:pkg timestamp_source:"ingest"` to see exactly which rows those
      are.

  Before #143 the sources were `read_from: end` + `ignore_older_secs: 600`, so a
  reset — and any outage longer than ten minutes — silently threw the window away
  instead. Duplicates you can see; a hole you cannot.

This is a recovery action, not maintenance. If you find yourself running it
regularly, the buffer is a symptom and something upstream (sink availability,
buffer sizing) is the actual bug.

## Verify

```bash
ssh root@192.168.25.15 'docker logs --tail 30 vector'   # no buffer errors
```

then re-run the `_time:5m | count()` query above and confirm it is non-zero, or
watch the `VictoriaLogs ingest stalled` rule return to Normal in Grafana.

## Related

- `ansible/roles/services/observability/README.md` — stack overview, data paths.
- [vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md)
  — the other way this pipeline dies quietly.

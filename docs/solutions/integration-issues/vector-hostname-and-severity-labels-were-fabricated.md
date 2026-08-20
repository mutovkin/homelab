---
title: "Vector's hostname was a container id and its log severity was fabricated"
date: 2026-08-20
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "hostname on every record was the container id (e.g. d7d7d4e8c59e), which changes on every recreate and churns _stream_id"
  - "every host record showed level info regardless of the log line's real severity"
  - "vector's own stderr was 88 percent template-render errors (216 of 245 lines in 30 minutes) for a missing container_name field"
  - "_time on every record was ingest time, never the log line's own timestamp"
  - "read_from end plus a 600 second ignore_older_secs turned any outage over 10 minutes into a permanent gap"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - vector
  - victorialogs
  - rsyslog
  - loki
  - ansible
tags:
  - vector
  - victorialogs
  - hostname
  - log-severity
  - rsyslog
  - loki
  - timestamp
  - observability
---

# Vector's hostname was a container id and its log severity was fabricated

## Problem

Host log ingestion into VictoriaLogs started working on 2026-08-18, and the data it
finally produced showed the *records* were close to unusable. Two headline defects,
and every obvious fix for the second one was a trap.

**`hostname` was the container id.** `parse_host` and `parse_docker` both called
`get_hostname!()`, which runs inside the vector container, so every record — host
and docker alike — was labelled `hostname="d7d7d4e8c59e"`. That is not a host, and
it changes on every container recreate, which churns `_stream_id` in VictoriaLogs.
Per-host alerting cannot be built on a label that does not identify a host.

**Severity was hardcoded.** Issue #143 asked to "parse rsyslog's line format" for
the priority. It does not carry one. Debian writes `/var/log/syslog` and
`/var/log/auth.log` in `RSYSLOG_FileFormat`:

```
2026-08-19T20:10:30.404106-07:00 deb-docker systemd[410669]: Startup finished in 48ms.
```

There is no `<PRI>` anywhere in that line — facility and severity are ABSENT from
the source, not merely awkward to extract. `parse_host` therefore hardcoded
`.level = "info"` on every host record, so an `emerg` and a routine "session opened"
were stored identically.

Two more defects rode along in the same file: the `add_timestamp` transform
formatted the event timestamp to a string the sink then ignored, so every stored
`_time` was ingest time; and the loki sink's fixed `container_name:
"{{ container_name }}"` label template could not render for host events, flooding
vector's own stderr.

## Symptoms

- Every record in VictoriaLogs carried `"hostname":"d7d7d4e8c59e"` and
  `"host":"d7d7d4e8c59e"` — the container id, under *both* keys. The issue named
  only `hostname`; fixing that alone would have left the same wrong value in `host`.
- Every record carried `"level":"info"`, host and docker alike — no way to alert on
  or filter by real severity.
- `_time` did not match the time embedded in `_msg`. Measured before the fix: a line
  whose own timestamp read `20:21:04.808674-07:00` was stored at
  `_time 2026-08-20T03:21:11.831063409Z` — seven seconds of file-tail lag, i.e. the
  record was stamped when Vector read it, not when it was logged.
- Vector's own stderr was 88% label-render warnings (216 of 245 lines in a 30-minute
  window; 1069 of the last 2000 lines) — one per host-log event, because
  `container_name` could never resolve for a source with no container.
- `/var/log/kern.log` was in the source glob although there is no kernel ring buffer
  in an LXC, implying kernel coverage the host does not have.
- `read_from: end` plus `ignore_older_secs: 600` meant any Vector outage longer than
  ten minutes was never backfilled even though the files on disk were intact.

## What Didn't Work

- **Parsing the existing syslog files harder.** There is nothing to parse — Debian's
  default format has no priority field. No amount of VRL cleverness recovers a byte
  that was never written. Severity has to be *made to exist* at the source, not
  extracted after the fact.
- **`journald` as the escape hatch.** Vector's `journald` source shells out to the
  `journalctl` binary. Measured on the running container:
  `docker exec vector journalctl --version` → `exec: "journalctl": executable file
  not found in $PATH` (rc 127). And against the image itself:
  `docker run --rm --entrypoint /bin/sh timberio/vector:latest-distroless-static -c
  "command -v journalctl"` → `exec: "/bin/sh": stat /bin/sh: no such file or
  directory` (rc 127). `timberio/vector:latest-debian` does not ship journalctl
  either. Closing the boot-window gap means building a custom image, so it stayed a
  documented, deliberate coverage gap.
- **Mounting `/etc/hostname` for the hostname fix.** It yields `deb-docker`, the
  CT's OS hostname. That survives a recreate, but it is not the Ansible inventory
  name, so per-host alerting would need a second hand-maintained
  hostname↔inventory mapping. (`HOSTNAME` from the environment is worse: Docker sets
  it to the container id by default, silently reproducing the bug.)
- **Re-formatting the parsed timestamp the way the old code formatted `now()`.** The
  old line was `.timestamp = format_timestamp!(now(), format: "%+")` — a STRING.
  Vector's loki sink reads the event timestamp only when it is a timestamp TYPE and
  silently falls back to `Utc::now()` otherwise (it also strips the `timestamp` key
  from the body: `remove_timestamp` defaults true). Writing
  `.timestamp = format_timestamp!(parsed.timestamp, format: "%+")` over the newly
  parsed value would compile, run, and look identical to a fix while changing
  nothing. **This is the dangerous one**: it reads as an obviously-fine refactor of a
  line that was already there, and no container-level check can tell the difference.
- **Giving host records a synthetic `container_name`** to stop the render warnings.
  Host events genuinely have no container. Inventing a placeholder trades a visible
  warning for a permanently wrong stored value in a real field.

## Solution

**1. Pass the real hostname in from Ansible.**
`ansible/roles/services/observability/templates/env.j2` emits
`VECTOR_HOSTNAME='{{ inventory_hostname }}'`; `compose.yaml` passes it with
`${VECTOR_HOSTNAME:?...}` (`:?`, never `:-` — an empty default would silently
relabel every record with `""`, and a label that is wrong-but-present is the defect
being replaced); `vector.yaml` reads `${VECTOR_HOSTNAME}` in every transform and
sets **both** `.hostname` and `.host`. Deliberately the inventory name and not the
OS hostname, for the reason in *What Didn't Work*. The OS hostname is not lost — it
is preserved per record as `syslog_hostname`, parsed out of the RFC5424 header.

This works at all because vector already runs with
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION: "true"`, added during the 0.57
fallout — see
[vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md).

**2. Make severity exist at the source, in a second stream.** An Ansible-deployed
drop-in `/etc/rsyslog.d/40-vector-structured.conf`
(`ansible/roles/services/observability/files/rsyslog-vector-structured.conf`)
re-emits every message a SECOND time in RFC5424
(`template="RSYSLOG_SyslogProtocol23Format"` — `<PRI>1 TS HOST APP PROCID MSGID SD
MSG`) to `/var/log/structured.log`. `vector.yaml`'s `host_syslog` source tails *that*
instead of `syslog` + `auth.log`, and recovers the metadata with `parse_syslog()`:

```vrl
parsed = parse_syslog(string!(.message)) ?? {}
if length(parsed) > 0 {
  .level           = parsed.severity
  .facility        = parsed.facility
  .appname         = parsed.appname
  .procid          = parsed.procid
  .syslog_hostname = parsed.hostname
  .timestamp       = parsed.timestamp
  .message         = parsed.message
} else {
  .level = "info"
  .parse_failed = "syslog"
}
```

(`?? {}` rather than a fallible assignment: `parse_syslog`'s error branch returns
null, and indexing null is a VRL *compile* error.)

Three details that make the drop-in correct rather than merely plausible:

- `*.*` in the drop-in equals Debian's `*.*;auth,authpriv.none` (→ syslog) PLUS
  `auth,authpriv.*` (→ auth.log) combined. One file replaces both — and `vector.yaml`
  must NOT also tail `syslog`/`auth.log`, or every host event is ingested twice.
- rsyslog rules are non-terminating, and `$IncludeConfig /etc/rsyslog.d/*.conf` sits
  at line 45 of `/etc/rsyslog.conf`, before the RULES section at line 53 — so the
  human-facing files keep being written, unchanged, in their existing format, for
  whoever is reading them over SSH during an incident. Verified after deploy:
  `/var/log/syslog` still growing, last line
  `2026-08-19T20:39:52.628029-07:00 deb-docker systemd[1]: Started session-320.scope`,
  while `/var/log/structured.log`'s last line was
  `<87>1 2026-08-19T20:39:52.633374-07:00 deb-docker sshd 431418 - - pam_env(...)`.
- The role runs `rsyslogd -N1` (validating the full merged config including the new
  drop-in) BEFORE restarting rsyslog, then hard-asserts `/var/log/structured.log`
  exists and is non-empty. That assert is load-bearing **because the same change
  removed the old inputs**: a drop-in that loads cleanly but routes nothing would
  leave the host shipping zero logs while every container stayed `Up` and the play
  stayed green. The config-error case is loud; the routing-error case is not.

Growth is bounded by an Ansible-deployed `/etc/logrotate.d/vector-structured`
(`files/logrotate-structured`): weekly, `rotate 4`, `postrotate` calling Debian's own
`/usr/lib/rsyslog/rsyslog-rotate` so rsyslog reopens the file instead of writing to an
unlinked inode.

**3. Keep the timestamp a timestamp, and fabricate one only when nothing upstream
supplied it.**

```vrl
ts = .timestamp
if !is_timestamp(ts) {
  .timestamp = now()
}
```

`.timestamp = parsed.timestamp` (a real value from the RFC5424 header) reaches this
transform unmodified for host records; docker records keep vector's native docker
timestamp; package-audit records have none and fall through to `now()`.

This became mandatory rather than cosmetic because the same change flipped both file
sources to `read_from: beginning` and dropped `ignore_older_secs`, to backfill the
package-audit logs. Under the string form the whole 293-line backfill would have been
stamped with the deploy time — a falsified history, shipped by the fix.

**4. Replace the fixed label templates with per-event dynamic expansion.** The
`finalize` transform builds a labels object and adds `container_name` only when the
event has one:

```vrl
labels = {"source": to_string!(.source), "hostname": to_string!(.hostname)}
if exists(.container_name) {
  labels = set!(labels, ["container_name"], to_string!(.container_name))
}
.loki_labels = labels
```

and the sink declares `labels: {"*": "{{ loki_labels }}"}` instead of three fixed
templates. Host events then simply carry no `container_name` label and there is
nothing to fail to render. Accepted by vector 0.57 — `vector validate
--skip-healthchecks` → `√ Loaded / √ Transforms configuration / √ Component
configuration / Validated`, rc 0. The sink already carried
`dangerously_allow_unconfined_template_resolution: true` from the 0.57 fallout, which
also covers the `"*"` expansion, so nothing new had to be opted out.

`severity` is stored as a plain JSON field, not a Loki label: VictoriaLogs makes body
fields queryable (`level:err` works), so labelling it would have multiplied
`_stream` cardinality by eight for nothing.

## Why This Works

Each fix closes a different gap between what looked true and what a live measurement
showed.

- Severity was never a parsing problem — it was a data-does-not-exist problem. The
  fix does not parse harder; it makes rsyslog write the missing byte a second time,
  in a format that carries it, alongside untouched human-facing files.
- The hostname fix moves the source of truth outside the container. `get_hostname!()`
  answers a question about the *process*, and the label needed to answer a question
  about the *machine*; nothing inside the container knows that answer, so it has to
  be injected at deploy time.
- The timestamp bug is invisible from the config alone: `format_timestamp!()` is a
  completely ordinary, correct-looking VRL call. It only breaks because the loki
  sink's timestamp handling is type-sensitive and silently degrades instead of
  erroring — the same "fails at the wrong layer" shape as vector-057's env-var
  interpolation bug, where the pipeline runs clean and only the destination is wrong.
- The stderr flood was real, but its assumed cause was not. #143 stated it was
  "self-amplifying" — vector's `docker_logs` source re-ingesting its own noisy
  stderr. `docker_logs` auto-excludes vector's own container: 7 days of VictoriaLogs
  held **zero** records with `container_name:vector` and **zero** containing "Failed
  to render template". Checking it cost one query and prevented fixing the wrong
  mechanism.

## Verification

End state proven from the destination's own output, never from container state:

```
# before
{"_msg":"2026-08-19T20:21:04.808674-07:00 deb-docker systemd[1]: session-215.scope: Deactivated successfully.",
 "_stream":"{hostname=\"d7d7d4e8c59e\",source=\"host\"}","file":"/var/log/syslog",
 "host":"d7d7d4e8c59e","hostname":"d7d7d4e8c59e","level":"info","source":"host"}

# after
{"_msg":"Removed session 320.","_stream":"{hostname=\"eq12_docker\",source=\"host\"}",
 "_time":"2026-08-20T03:39:52.674418Z","appname":"systemd-logind","facility":"auth",
 "file":"/var/log/structured.log","host":"eq12_docker","hostname":"eq12_docker",
 "level":"info","procid":"130","source":"host","syslog_hostname":"deb-docker"}
```

- **Severity varies, from real lines.** `_time:3m source:host | stats by (level) count()`
  → `debug` 6, `info` 449, `err` 2. The `err` record is
  `imklog: cannot open kernel log (/proc/kmsg): Permission denied.` from `rsyslogd`,
  facility `user` — itself the proof that there is no kernel ring buffer in an LXC
  and `/var/log/kern.log` can never exist there.
- **`_time` is the line's own time, to the microsecond.** Raw:
  `<86>1 2026-08-19T21:00:48.473641-07:00 deb-docker sshd 446661 - - pam_unix(...)`.
  Stored: `_time 2026-08-20T04:00:48.473641Z`. Identical. Before the fix the same
  comparison showed a 7-second ingest-lag offset.
- **Package-audit records are the documented exception**: all 293 landed inside a
  3.8 ms window (`min(_time)` …T03:39:20.421Z, `max` …20.424Z) while their `_msg`
  values carry dates back to 2026-08-16. Their in-line timestamps are not
  syslog-shaped, so they keep the file source's ingestion time by design.
- **Label-render warnings: 216 in 30 minutes → 0** in the five minutes after.
- **Hostname survives a deliberate recreate** (the whole point of the fix): container
  id `d7d7d4e8c59e…` → `b279c34480272cdeedabade50108bebc8253b12d0c70ecfa55edc3d67960b431`,
  `hostname` still `eq12_docker`, and `_stream_id` unchanged at
  `0000000000000000ee5a1f009399851b5ed8b7cadf597c8b` across the recreate — the churn
  the issue described is gone, not merely relabelled.

## Prevention

- **Verify a log-format assumption by looking at an actual line before planning to
  parse it.** "rsyslog carries the priority" was the issue's premise and was wrong.
  One `tail -1 /var/log/syslog` would have caught it before any VRL was written.
- **When a change swaps a data source, the assert must be on the NEW source producing
  data, not on the process being up.** The role checks `/var/log/structured.log` is
  non-empty, not that `rsyslog.service` is active — a drop-in that loads but routes
  nothing leaves rsyslog running and Vector's tailed file empty, and only the former
  shows up in a naive health check.
- **A directive that produces a value nothing consumes is invisible.** Before trusting
  a written field, check whether the sink actually reads it, and *in what type*.
  `format_timestamp!()` produced a perfectly valid string the loki sink's type check
  discarded. Verify with the destination's own introspection — here, comparing a
  stored `_time` against the source line's embedded time — not with "the field is
  present in the transform".
- **Prove a remap functionally against sample input before deploying it.** A throwaway
  `vector` invocation (`stdin` source, `console` sink, the identical remap) was fed
  five hand-written RFC5424 lines and returned `level` values
  `info`/`err`/`notice`/`warning` plus a `parse_failed: "syslog"` fallback record —
  proving severity, facility, appname, procid, syslog_hostname and the parsed
  timestamp all land, and that the failure branch works, without touching the running
  service. The staging and cleanup went through `ansible -m copy` / `-m file
  state=absent`, so no ad-hoc SSH mutation was involved.
- **A stated root cause in an issue is a claim, not a given — check it when checking is
  cheap.** The "self-amplifying noise loop" claim cost one query to falsify. The flood
  was still worth fixing; the wrong mechanism would have led to fixing the wrong thing.
- **A label whose value comes from inside the container answers a question about the
  container.** If the label has to identify a host, inject it from the deploy layer
  and guard it with `${VAR:?}` so a missing value fails the parse instead of labelling
  everything `""`.

## Related Issues

- #143 — this fix. #134 (fleet-wide vector) is what depends on the `hostname` label
  being the Ansible inventory name.
- [vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md)
  — same pipeline, same failure shape: Vector runs clean while the destination is
  wrong, and only a query against the destination proves it. Both
  `VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION` (which makes `${VECTOR_HOSTNAME}`
  resolve) and `dangerously_allow_unconfined_template_resolution` (which covers the
  `"*"` label expansion) come from there.
- [vector-wedged-disk-buffer-reset.md](vector-wedged-disk-buffer-reset.md) — the
  `read_from: beginning` / no-`ignore_older_secs` change exists so a buffer reset
  (which wipes checkpoints) re-reads intact on-disk logs instead of losing them. The
  cost is bounded re-ingestion of at most one logrotate period, which is why that
  policy's cadence matters. That doc's "What it costs" section was updated in the
  same change.

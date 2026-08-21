# rsyslog_structured

Makes the host emit **every** syslog message a second time in RFC5424 to
`/var/log/structured.log`, and proves — every run — that it is doing so right now.

That file is the single host-log input for every Vector deployment in this fleet.

## Why this role exists at all

Debian writes `/var/log/syslog` and `/var/log/auth.log` in `RSYSLOG_FileFormat`,
which carries **no `<PRI>` field**. Severity and facility are therefore not in
those lines and cannot be parsed out of them, so Vector had to hardcode
`level: info` on every host record — an `emerg` was stored identically to a
routine "session opened" (#143).

Severity cannot be recovered; it has to be **made to exist first**. This role
deploys `/etc/rsyslog.d/40-vector-structured.conf`, whose `*.*` selector re-emits
the same messages in `RSYSLOG_SyslogProtocol23Format`, where `<PRI>` is
`facility*8+severity`. Vector's `parse_syslog()` then yields `level`, `facility`,
`appname`, `procid`, `syslog_hostname` and the line's own microsecond timestamp.

Growth is bounded by `/etc/logrotate.d/vector-structured` (weekly, `rotate 4`,
`maxsize 100M`), deployed alongside.

## Why it is a shared role (#134)

It was born inside `roles/services/observability`, because eq12_docker was the only
host running Vector. The drop-in's own FLEET NOTE predicted the move; #134 took
Vector fleet-wide and made it. This is **host-log plumbing**, not
observability-stack service config, and it now has two consumers:

| Consumer | Hosts | Vector shape |
| -------- | ----- | ------------ |
| `roles/services/observability` | eq12_docker | container, in the compose stack |
| `roles/vector_agent` | eq12, n5pro, n5pro_docker | native systemd unit |

Both `include_role` it, and both do so **before** pointing Vector at the file.
That ordering is load-bearing: this role's freshness probe is the gate that says
the stream is genuinely live, and Vector reads only this file for host logs.

Editing `files/rsyslog-vector-structured.conf` changes host logging on **all four
hosts at once**.

## What it does, in order

1. Copies the rsyslog drop-in and the logrotate policy.
2. `rsyslogd -N1` — parses the whole config (every drop-in in `/etc/rsyslog.d`,
   not just ours) without touching the running daemon. Verified to exit non-zero
   on a **parse** error; semantic errors are *not* confirmed to fail it, which is
   why step 5 exists.
3. Restarts rsyslog, but only when the drop-in itself changed.
4. Ensures rsyslog is running and enabled — **ungated**, no `changed` condition.
   If rsyslog is dead on an otherwise-converged host, nothing else would start it
   and Vector's only host-log input silently stops growing while every task
   reports `ok`.
5. **The end-to-end freshness probe.** Writes a unique marker with
   `logger -p local0.notice` and waits up to 10 s for that exact marker to appear
   in `/var/log/structured.log`, then asserts. This is the whole path: rsyslog is
   alive, it is reading its socket, the `*.*` selector still reaches our action,
   and `omfile` is still writing to this path. "The file exists and is non-empty"
   proves none of that — a file untouched since last week satisfies it instantly.

   > **This guard was INERT for its entire life before #134.** The `wait_for`
   > carried `failed_when: false`, which does not "let the result through" — it
   > *assigns* `failed: False`. The assert that reads `is not failed` was therefore
   > literally `assert: true`, and a `wait_for` that timed out passed it. Measured:
   > `failed_when: false` → `failed=False, elapsed=3` (assert passes) versus
   > `ignore_errors: true` → `failed=True` (assert fires). It is `ignore_errors`
   > now. The guard can genuinely fail from here on, on all four hosts, and it fails
   > in the safe direction — the play stops *before* Vector is pointed at a stream
   > nothing is writing. If it fires, that is a finding about that host, not a
   > reason to put `failed_when: false` back.

Everything from step 2 onward is skipped under `--check`.

## The host-log heartbeat (#134)

Every host emits one marker line every **5 minutes**:

```
homelab-heartbeat <inventory_hostname>      # logger -p daemon.info
```

driven by `homelab-heartbeat.timer` → `homelab-heartbeat.service`, both templated
by this role. `obs-host-log-ingest-stalled-per-host` alerts when a host misses
**four consecutive beats** (a 20-minute window).

### Why a heartbeat instead of just watching the logs

Because on this fleet, "no host log lines arrived" is not evidence of anything.
Measured 2026-08-20:

| Host | empty 10m windows | empty 15m | max gap |
| ---- | ----------------- | --------- | ------- |
| eq12_docker | **66.2%** (VictoriaLogs: 68.1%) | 57.5% | 3600.0s |
| eq12 | **39.7%** | 33.8% | 3600.0s |
| n5pro | 0.0% | 0.0% | 448s |
| n5pro_docker | 0.0% | 0.0% | 396s |

Host logs arrive in **bursts** with long silences between them — one 10-minute
bucket held 1288 records, another held 1. An alert built on that silence paged a
human roughly every 30 minutes for 21 hours while the fleet was perfectly healthy.

Two things that look like fixes and are not:

- **Widening the window.** The max gap on both offending hosts is *exactly*
  3600.0s. That is not headroom — it is a single incidental hourly event holding
  the window open (on eq12_docker, recurring `dockerd` image-signature **errors**).
  A 60-minute window would be one bugfix away from flapping, and would cost ~70
  minutes of detection latency.
- **Per-host tuning.** Burstiness does not track volume: the *busiest* host is the
  worst offender at 66%, while n5pro never had a single empty 10-minute window.
  There is no per-host number to tune toward.

A heartbeat replaces an ambiguous signal (absence of incidental traffic) with an
unambiguous one (absence of something contractually always present), and behaves
identically on a busy container host and an idle hypervisor.

### What it does and does not prove

The beat travels the **full** path — `logger` → `/dev/log` → rsyslog's `*.*`
selector → `omfile` → `/var/log/structured.log` → Vector's file source → the sink
→ VictoriaLogs. So it exercises the same machinery a real host log line does, and
`daemon.info` is deliberately an ordinary facility rather than a dedicated one: a
private facility would be a shorter path and would prove less.

**The caveat, stated rather than left implicit:** it monitors the *heartbeat*
path, not the *application-log* path. An rsyslog rule that dropped `daemon.info`
while still routing other facilities would keep this alert quiet. That failure
mode is narrow, and it is already covered from the other side — the per-deploy
freshness probe above writes its marker at `local0.notice` through the same `*.*`
selector, so the two together cover both a broken selector and a
facility-specific rule.

### The contract, and where it can silently break

The marker string exists in **two files** and nothing but the role's own probe
would notice them drifting apart:

| Where | What |
| ----- | ---- |
| `defaults/main.yml` | `vector_heartbeat_marker` |
| `roles/services/observability/.../alerting/per-host-ingest-health.yaml` | the `"homelab-heartbeat"` filter in the LogsQL |

Change one, change both, in the same commit. If they drift, **every host reads as
dead**. The role asserts, on every run, that a beat actually reaches
`structured.log` — that assert is what turns a silent contract break into a failed
deploy.

Two deliberate details in the units:

- **`Persistent=false`** on the timer. A heartbeat is a liveness signal, not an
  audit record; catching up missed beats after downtime would write stale markers
  claiming the host was alive when it demonstrably was not.
- **`After=rsyslog.service`, never `Requires=`.** If rsyslog is down the beat
  should be *lost* — that is exactly what the alert must see. A hard dependency
  would stop the beat from running and produce the same silence for a different
  reason, which is strictly less informative.

## Interface

No variables. It is deliberately parameterless — a second structured stream with
different settings would mean two files Vector could disagree about.

It exports one fact the caller may read after the include:

| Fact | Meaning |
| ---- | ------- |
| `vector_structured_probe` | the unique marker string this run wrote through `logger`. Undefined in check mode. |

Variables (all with defaults, see `defaults/main.yml`):

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `vector_heartbeat_enabled` | `true` | deploy the heartbeat timer at all |
| `vector_heartbeat_interval` | `5min` | beat spacing; the alert window is 4x this |
| `vector_heartbeat_accuracy` | `10s` | `AccuracySec` — systemd's 1min default drifts too far against a 20m window |
| `vector_heartbeat_marker` | `homelab-heartbeat` | the string the alert filters on |
| `vector_heartbeat_priority` | `daemon.info` | ordinary facility on purpose |

The register `vector_rsyslog_conf` (the drop-in copy) also survives the include,
and is what the internal restart is gated on. Callers should not gate on it —
they should gate on their **own** config templates.

## The two things this role does NOT do

- It does not tail anything. Vector does that; see the `host_syslog` source in
  `roles/services/observability/files/data/vector/vector.yaml` and in
  `roles/vector_agent/templates/vector.yaml.j2`.
- It does not stop `/var/log/syslog` and `/var/log/auth.log` being written. Its
  rules are non-terminating (no `stop`) and `$IncludeConfig` runs before the RULES
  section of `/etc/rsyslog.conf`, so the human-facing files keep their existing
  format. That is deliberate — those are what an operator reads over SSH during an
  incident. The cost is that `/var/log` write volume roughly doubles (+4.2% for the
  RFC5424 header over syslog+auth combined, measured).

**Vector must never tail `syslog`/`auth.log` as well as this file.** `*.*` is
exactly Debian's `*.*;auth,authpriv.none` plus `auth,authpriv.*`, so their union
is already here; tailing both ingests every host event twice.

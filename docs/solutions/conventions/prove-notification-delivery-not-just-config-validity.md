---
title: "Validation passing is not delivery: prove an alerting channel with a forced send, and rank the evidence honestly"
date: 2026-08-19
category: conventions
module: services/watchtower
problem_type: convention
component: tooling
severity: high
applies_when:
  - "bumping the version of anything that sends notifications, alerts, or email"
  - "changing a notification URL, SMTP credential, or alert receiver"
  - "adding a new service whose only failure signal is a notification"
  - "a container is healthy with RestartCount 0 and that is being read as proof the channel works"
  - "writing up evidence that an alerting path is working"
symptoms:
  - "A notification channel has not delivered a message since the last change to it, and nobody can say whether it still works"
  - "A version bump tightened notification validation, and 'the container came up healthy' was taken as reassurance"
  - "The routine run never emits a message because there is nothing to report, so the send path is never exercised"
related_components:
  - watchtower
  - shoutrrr
  - observability
  - docker-compose
tags:
  - notifications
  - alerting
  - verification
  - watchtower
  - shoutrrr
  - smtp
  - silent-green
---

# Validation passing is not delivery: prove an alerting channel with a forced send, and rank the evidence honestly

## Context

Watchtower was bumped 1.20.3 → 1.21.0 (#136). The release tightened notification field
validation and moved shoutrrr to v0.17.1. Both containers came up **healthy with
RestartCount 0**, and that was read as reassurance that notifications still worked.

It is not. A healthy start proves the `smtp://` URL **parses** — a malformed URL fails
fast at startup, so the container coming up is exactly and only evidence about parsing.
Nothing had exercised an actual send since the bump, because Watchtower only notifies when
it has something to report and every scanned container was current or digest-pinned
(`eq12_docker` 12/12 updated=0, `n5pro_docker` 3/3 updated=0).

The gap is silent by construction: **a notification that never arrives is
indistinguishable from "nothing needed updating."** And it costs the most exactly where
the project relies on it — for the [monitor-only](../../../CONCEPTS.md) class (postgres,
joplin, vaultwarden, grafana, immich, portainer, watchtower itself) the email *is* the
update mechanism, with no auto-apply to fall back on. Broken mail silently converts the
deliberate-update path into a no-update path.

## Guidance

### 1. Rank the evidence, and never write a lower rung as a higher one

| Rung | What it proves | What it does NOT prove |
| ---- | -------------- | ---------------------- |
| **Config parses** — container starts, RestartCount 0, no validation error | the URL/credential is syntactically acceptable | that a message is ever built, sent, or accepted |
| **Send attempted, no error logged** | the code path ran | that anything left the host — absence of an error is not a positive |
| **Positive send confirmation** — the sender's own success line | the remote server accepted the message | that it was routed to an inbox rather than dropped or spam-filed |
| **Recipient confirmed** — the message is in the destination inbox | end-to-end delivery | — |

"No error logged" is a **weaker** claim than "send confirmed" and must never be written up
as the latter. If a build only logs on failure, say so explicitly.

### 2. Force a send rather than waiting for one

Do not wait for a real event to exercise the channel — that is what leaves the gap open
for weeks. Force a message that is guaranteed to exist regardless of whether anything
happened, and prefer a forcing mechanism that does **not** depend on a report template.

For Watchtower, that is the **startup message**: production sets
`WATCHTOWER_NO_STARTUP_MESSAGE=true`, and a throwaway probe simply omits it, so the
startup notification is queued and flushed through shoutrrr at shutdown.

```bash
# Take the URL from the RUNNING container — that is the value the service actually uses,
# and it is stronger evidence than reading the templated .env off disk.
# Exported and passed as a bare `-e NAME` pass-through so it never enters the argv.
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

Invariants worth copying to any probe of this shape: `--rm` so it is discarded;
`--monitor-only` so the probe can never change anything; `--security-opt
apparmor=unconfined`, without which nothing starts on these Docker-in-LXC hosts; no
`watchtower.enable` label so the probe stays out of its own scan; and **every captured
line through a redactor** before it is written, pasted, or quoted.

### 3. Do not build the probe on the report template

This is the sharpest measured detail. Running the identical probe on both hosts in the
same minute, the report path **diverged**:

```
eq12_docker    Template processing completed successfully  entries_count=1  msg_length=54
n5pro_docker   Template processing completed successfully  entries_count=0  msg_length=0
n5pro_docker   Message empty, skipping send
```

…and yet **both hosts sent exactly one mail**, because the startup message is queued
separately from the session report and drained at shutdown. A probe built on the report
template would have proved delivery on eq12_docker and **silently proved nothing** on
n5pro_docker — while looking identical in a summary. Prefer the forcing path that does not
depend on there being something to report.

### 4. Probe every host, not one representative

Hosts share the URL and the image but not the network path. Egress to the mail provider
from `n5pro_docker` is a genuinely separate fact from `eq12_docker`, and a firewall or
route change touches one without the other.

### 5. Read the credential from the running process, and keep it out of everything

- Read it from the **running container**, not from the templated `.env` — that proves the
  value the service is actually using.
- Pass it as a bare `-e NAME` pass-through (inherit from the environment), never
  `-e NAME=value`, so it does not appear in the host's process argv.
- Redact every captured line. Debug logging is verbose and prints more than you expect.
- **Do not use `docker run --env-file /data/deploy/<svc>/.env`.** Those files single-quote
  every value on purpose (#117 — compose's dotenv parser interpolates `$` in unquoted
  values). Compose's dotenv parser strips the quotes; **the docker CLI's `--env-file`
  parser does not.** The sender would receive a URL wrapped in literal `'…'`, fail to
  parse it, and you would misdiagnose a quoting bug as an upstream regression.

### 6. Make it a required post-bump step, in writing

The check only compounds if the next person runs it. It is now part of the bump procedure
and the bump log in `ansible/roles/services/watchtower/README.md`, not a thing someone
might remember.

## Why This Matters

This is the project's [silent-green failure](../../../CONCEPTS.md) shape applied to the
one control that reports on all the others. When an alerting channel breaks, the symptom
is *silence* — which is also what "everything is fine" looks like. There is no failing
run, no drift, no error to notice.

It compounds with the monitor-only posture: a service in that class is deliberately not
auto-updated, on the explicit promise that an operator will be told when an update exists.
If the telling is broken, the class quietly becomes "never updated," and the first
evidence is a security advisory rather than an email.

The same reasoning covers any channel whose only output is a message nobody is waiting
for — alert receivers, on-call routing, backup-failure mail, UPS shutdown notices.

## When to Apply

- After bumping any component that sends notifications — especially when release notes
  mention notification, validation, or transport-library changes.
- After rotating a credential or changing a notification URL, receiver, or SMTP host.
- After a network, firewall, or egress change on a host that sends.
- When adding a service whose failure mode is "you get an email."
- Before writing up any claim that an alerting path works.

## Examples

**Before** — the whole of the evidence, and it proves only the first rung:

```
$ docker inspect watchtower --format '{{.State.Status}} {{.RestartCount}}'
running 0
```

**After** — a forced send, on both hosts, with the sender's own positive confirmation
(credential redacted; probes ran 2026-08-20 03:24–03:25 UTC):

```
level=info  msg="Update session completed" failed=0 scanned=12 updated=0     # eq12_docker
Mail successfully sent to "REDACTED-ADDR"!
level=debug msg="Notification send completed successfully" total_urls=1
PROBE_EXIT=0

level=info  msg="Update session completed" failed=0 scanned=3 updated=0      # n5pro_docker
Mail successfully sent to "REDACTED-ADDR"!
level=debug msg="Notification send completed successfully" total_urls=1
PROBE_EXIT=0
```

`scanned=12` / `scanned=3` match the labelled counts measured the same run, so scan scope
is proved alongside delivery. Both live watchtower containers were untouched (still Up,
RestartCount 0, unchanged `StartedAt`), and no probe container was left behind.

That is rung 3. Rung 4 — the two mails actually appearing in the destination inbox —
remains the human's step, and the UTC timestamps above exist so they can be matched.

## Related

- #145 — the issue this convention came from
- #136 — the 1.20.3 → 1.21.0 bump that opened the gap
- #83 — the monitor-only posture class whose entire payoff is the notification
- #117 — the `.env` single-quoting that makes `--env-file` the wrong transport here
- [[watchtower-label-enable-scan-scope]] — the worked instance, and the container-level
  half of the same question. That doc answers "is it being watched?" (assert the scan
  count, not the exit status); this one answers "would we actually hear about it?" (assert
  a send, not a healthy start).
- [[vector-057-silent-log-pipeline-failure]] — #73, the first recorded instance of the
  class. "Up with RestartCount 0 is consistent with dropping 100% of events" is this same
  claim one layer down the stack.
- [[vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback]] — #117, why the
  `.env` is single-quoted and therefore why `--env-file` would hand the sender a literally
  quoted URL
- [[compose-up-recreates-watchtower-created-containers]] — why a docs-only watchtower
  change must not be "published" with a deploy
- [[ansible-change-loop-pitfalls]] — §6 "a skipped branch is an untested branch" is the
  same evidence-level error: a path nothing has exercised is a path nothing has proven
- [[pg18-restrict-slicing-silent-green-restore-drill]] — the sibling from the same batch:
  a *verification* that ran, reported success, and proved nothing
- `ansible/roles/services/watchtower/README.md` — the probe as a required post-bump step,
  plus the bump log

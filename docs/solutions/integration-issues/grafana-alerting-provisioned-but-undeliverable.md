---
title: "Provisioning Grafana alerting: the delivery path, the rsync that eats it, and a test endpoint with no documented request"
date: 2026-08-20
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "alert rules exist and evaluate, but the only contact point is Grafana's stock stub whose address is the literal example@email.com"
  - "docker compose rejects the whole file with invalid interpolation format for an environment key that looks correct"
  - "the documented per-receiver test endpoint returns 410 and its replacement returns 400 unknown integration type for every request body"
  - "a templated file inside an rsync'd directory is deleted and rewritten on every run, restarting the container every deploy"
root_cause: config_error
resolution_type: config_change
severity: medium
related_components:
  - grafana
  - telegraf
  - victoriametrics
  - ansible
  - docker-compose
tags:
  - grafana
  - alerting
  - provisioning
  - docker-compose
  - yaml
  - ansible
  - rsync
  - observability
---

# Provisioning Grafana alerting: the delivery path, the rsync that eats it, and a test endpoint with no documented request

## Problem

Wiring the first real notification channel into a file-provisioned Grafana (13.2)
surfaced four traps that are invisible until something is actually deployed or
actually sent. None is about the alert rules themselves — they are all about the
delivery path, which is the half that stays untested because a rule that never
fires never exercises it.

## Symptoms

- Four service-health probes were collected and stored for the life of the
  deployment with no rule anywhere, and the one rule that did exist routed to
  Grafana's stock `grafana-default-email` receiver, whose address is the literal
  placeholder `<example@email.com>`. A rule wired to that is indistinguishable from
  a rule wired to nothing.
- The first live apply died at the compose step:
  `invalid interpolation format for services.grafana.environment.GF_SMTP_USER. You
  may need to escape any $ with another $. ${GRAFANA_SMTP_USER:?required - see`
- `POST /api/alertmanager/grafana/config/api/v1/receivers/test` → **410**, naming a
  replacement endpoint. The replacement rejected every plausible request body with
  **400** `Invalid receiver: 'unknown integration type: ''`, and a bodyless POST
  (which is what Grafana's own UI client sends) returned **500** with `error=EOF`.

## What Didn't Work

- **Leaving the contact point unprovisioned** and calling the rules done. That is
  the defect one layer up: an alert rule with no deliverable contact point is the
  same silent failure as no rule, and it looks healthier because the rules list is
  populated.
- **Adding a second copy of the SMTP secret to the vault.** The host already has
  exactly one proven-working relay — the credentials Watchtower sends release
  notifications through. A second copy of the same app password rots independently
  and buys nothing. Reusing them is correct; the cost is a coupling, which has to be
  made loud rather than hidden (see Solution).
- **Putting the contact point in the same directory the role rsyncs.** The address
  is a vault value, so the file must be templated, not committed — but the
  provisioning directory is synchronised with `delete: true`. The rsync deletes the
  templated file every run, the template writes it back, both report `changed`, and
  Grafana restarts on every single deploy forever.
- **Guessing at the receiver-test request body.** Six shapes were tried against the
  replacement endpoint — the resource wrapper, the spec, a bare integration, an
  array — and all returned the identical 400, because the body was not being decoded
  at all. The endpoint's OpenAPI entry documents no request body, and reading
  Grafana's generated frontend client showed the UI sends none.

## Solution

**1. Quote every compose `${VAR:?message}` whose message contains a `#`.** An
unquoted YAML scalar ends at a ` #` — space-then-hash starts a comment — so

```yaml
GF_SMTP_USER: ${GRAFANA_SMTP_USER:?required - see #139}
```

reaches compose as `${GRAFANA_SMTP_USER:?required - see`, an unterminated
interpolation that fails the parse of the **whole file**. Quote the value:

```yaml
GF_SMTP_USER: "${GRAFANA_SMTP_USER:?required - see #139}"
```

A `#` that follows a non-space character is not a comment marker, which is why
`${VECTOR_HOSTNAME:?required - see .../env.j2 (#143)}` survives unquoted in the same
file. Do not rely on that: quote the value.

**2. Exclude the templated file from the `delete: true` sync, and make the template
drive the restart.** The provisioning synchronise gets
`--exclude=alerting/contact-points.yaml`; the template task runs *after* the sync,
carries `diff: false` (it contains an address), and its `changed` result joins the
restart-decision fact with **no `default()`** — a renamed register must fail loudly
rather than silently decide Grafana needs no restart. The precedent is the shared
deploy role's own `--exclude=.env`.

The check that proves it: a second, unchanged run must report the sync `ok`, the
template `ok`, no restart, and `changed=0`. If Grafana restarts on the second run,
the exclude is wrong.

**3. Make the shared-credential coupling loud.** The vault variable names are listed
by name in the role's existing "assert credentials are non-empty" loop at the top of
the play, so renaming one fails the run naming the variable, instead of producing a
Grafana that starts fine and can never send. `GF_SMTP_*` all use `:?`, never `:-`.

**4. The Grafana 13.2 receiver-test request body is `{"integration": {...}}`.** The
route is
`POST /apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/{name}/test`,
where `{name}` is the **base64 of the receiver title** (not its uid) and the
namespace is `default` for org 1. The accepted body is a single integration object:

```json
{"integration": {"uid": "...", "type": "email", "version": "v1",
                 "disableResolveMessage": false,
                 "settings": {"addresses": "..."}}}
```

→ `200 {"status":"success","duration":"1s141ms"}`, where the duration is the real
SMTP round trip.

How the shape was found, since no documentation carries it: POST a JSON **array**.
The server then fails with a decode error that names the Go type, and that appears
in the container log:

```
custom route handler failed method=POST path=test kind=Receiver
error="json: cannot unmarshal array into Go value of type v1beta1.CreateReceiverIntegrationTestRequestBody"
```

The field name follows from the type name. This is a general technique: when an API
rejects every guess with the same validation message, send a deliberately
wrong-shaped payload to force a *decode* error instead of a *validation* error, and
read the server's log rather than its response body.

## Why This Works

The compose failure and the rsync loop are the same class of problem: a construct
that is correct in isolation and wrong in composition. `${VAR:?msg}` is valid compose
syntax and `delete: true` is the right rsync posture for an Ansible-owned directory —
each becomes a bug only when combined with something else in the same file or the
same directory. Neither is visible in review; both are visible on the first apply and
the second run respectively, which is why both checks belong in the deploy loop
rather than in a reviewer's head.

The delivery-path traps share a different property: nothing fails. An unprovisioned
contact point, a stub address, and a working SMTP config that was never exercised all
present identically — a populated alert-rules list. The only distinguishing evidence
is a send that actually completes, which is why the test endpoint mattered enough to
reverse-engineer rather than skip.

## Verification

- Rules provisioned and **evaluating**, not merely loaded — `lastEvaluation` must
  advance across two reads. Fresh after a Grafana restart the probe rules read
  `0001-01-01T00:00:00Z` (never evaluated); at +2 min `20:48:50-07:00`; at +6 min
  `20:52:50-07:00`. A rule that loaded but never evaluates has a null or frozen
  timestamp, and that distinction is the whole point of the check.
- Contact point and root policy from Grafana's own API, not the file on disk:
  `/api/v1/provisioning/contact-points` shows the receiver with `"provenance":"file"`,
  and `/api/v1/provisioning/policies` shows it as the root `receiver`.
- Delivery: `200 {"status":"success", "duration":"1s141ms"}` plus the container log
  line `custom route handler succeeded method=POST path=test kind=Receiver`.
- The full query→reduce→threshold pipeline against live data, via
  `POST /api/v1/rule/test/grafana` with the rule's own `data` array and the threshold
  deliberately inverted so it must fire. It returned one firing instance per probed
  target, each carrying the grouping labels — which also demonstrates that a newly
  added probe target becomes its own alert instance with no rule edit.
  That endpoint returns **403 Access denied** without a `folderUid` in the payload;
  the permission check is folder-scoped.
- Second unchanged run: `changed=0`, restart task skipped, container uptime unchanged.

## Prevention

- **Quote any compose value containing a `#`.** The failure is loud but it fails the
  whole file, and the truncation point (`… - see`) is the tell.
- **A templated file inside a `delete: true` sync needs an exclude, and the pair needs
  an idempotency test.** "Second run reports no change" is the only check that catches
  a delete/rewrite loop; a single successful apply looks identical.
- **Prove the notification channel, not just the rule.** An alert rule is only as good
  as a send that completed. Where the platform has no usable test endpoint, that is a
  finding to report, not a step to skip.
- **When an API rejects every guess identically, force a decode error.** A type-name in
  a server log is often the only available documentation.
- **Absence should be alerted on exactly once.** When several rules watch the same
  signal, one of them owns "no data at all" and the others treat no-data as OK —
  otherwise a single dead collector pages once per rule. **And the owner's own series
  must itself be continuously exported in health** — verify by sampling it, do not assume
  it from the metric's name. A counter that is only exported once non-zero cannot carry
  an absence rule; #152 nearly shipped one that would have fired permanently. See
  [absence-alerts-need-a-continuously-exported-sentinel](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md),
  and
  [instant-query-cannot-prove-a-series-is-live](../conventions/instant-query-cannot-prove-a-series-is-live.md)
  for why an instant query cannot do that verification.

## Related Issues

- #139 — this work. #108 added the first alert rule and explicitly left the contact
  point for later; that "later" is what this closes.
- [vector-057-silent-log-pipeline-failure.md](vector-057-silent-log-pipeline-failure.md)
  — the same shape one layer down: a pipeline that reports healthy while its output
  goes nowhere.

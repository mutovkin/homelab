---
title: "Drill alerts identify themselves in the notification and cannot deploy without an operator-granted window"
date: 2026-08-29
category: conventions
module: services/observability
problem_type: convention
component: monitoring
severity: high
applies_when:
  - "a verification needs a real alert to fire and a real notification to be delivered"
  - "you are about to push a synthetic series into a live alert rule"
  - "you are adding a temporary alert rule that exists only to be exercised"
  - "an agent is running an alerting change end-to-end without a human watching each step"
  - "a rule uid or a metric label value in the repo looks synthetic and nobody remembers why"
symptoms:
  - "A notification arrives that reads exactly like a real incident and turns out to be a test"
  - "The operator asks whether an alert was real"
  - "A synthetic probe's only marker is a label value the reader must already know is fake"
  - "An alert rule with an obviously temporary uid is still being served by Grafana weeks later"
  - "A drill was 'made safe' with isPaused or a Grafana silence and proved nothing / expired quietly"
related_components:
  - grafana
  - victoriametrics
  - alertmanager
tags:
  - grafana
  - alerting
  - verification
  - synthetic-probe
  - drill
  - safety
  - consent
  - notification-templates
---

# Drill alerts identify themselves, and cannot fire without an operator window

## Context

Verifying #188 required proving that two drives crossing the thermal threshold together
produce **two** notifications rather than one merged alert naming neither. A config diff
showing `group_by` changed proves nothing; the only honest proof is to make alerts fire and
count what is delivered. So synthetic `truenas_disk_temperature_celsius` samples were pushed
at 48-49 C under fabricated devnames `probe188c` / `probe188d`.

The verification worked, and it produced the strongest evidence artefact this repo has:
before the fix, one merged send per channel; after, two, one per drive, read from Grafana's
own `/metrics` counter.

**It also paged the operator at ~3am, and the alert was indistinguishable from a real one.**
Nothing in the text said "test". The only clue was a `devname` the reader would have had to
recognise as synthetic — which requires already knowing a verification run was happening.
The operator had to ask whether their array was overheating. (#187 had just rewritten every
notification to be short and human, which makes them *more* trusted and therefore more
costly to fake.)

Two defects, not one: the probe carried no marker, and nobody was told a drill would fire.

This is not a one-off. Four agents across this batch invented four different ad-hoc safety
schemes for the same problem, and one leaked: #206's fixture reached `state=pending`
carrying no marker at all — a rule one evaluation away from a notification nobody could
have identified. The improvisation is the failure mode. Hence a convention.

## The convention

### Two drill shapes, two markers

A **drill RULE** — a temporary rule that exists only to be exercised — carries **both**:

```yaml
uid: obs-drill-<issue#>-<slug>      # greppable, here and in Grafana
labels:
  drill: "<issue#>"                 # renders the marker into the notification
```

A **drill SERIES** — a synthetic sample pushed into a REAL rule, where the whole point is
exercising the real rule and the real routing — cannot carry a `drill` label: the rule's
`by (...)` clause drops every label name it does not list. Its marker is therefore the
**identity label's VALUE**:

```
devname="probe201a"   host="probe217a"   ...   probe<issue#><letter>
```

That value survives aggregation, and the notification policy's `group_by` carries every
identity label, so the value is always in `.CommonLabels` for the group. This was already
the repo's de-facto naming (`probe188c`, `probe188d`, `synthetic-217-probe`); the convention
makes it load-bearing and canonical — `probe<issue#><letter>`, no other spelling.

### The marker is decided at the template layer

`ansible/roles/services/observability/files/data/grafana/provisioning/alerting/notification-templates.yaml`
defines `homelab.drill_marker`, which renders `[DRILL #<issue>] ` for the label shape and
`[DRILL] ` for the probe-value shape, and nothing otherwise. `homelab.subject` (every email
receiver's `subject:`) and `homelab.message` (every telegram receiver's `message:`) prefix
it. Deciding it there is the point: **no per-probe step can forget it.**

Resolve notices render through the same templates, so they carry the marker too. An unmarked
"resolved" re-raises the same was-that-real question in reverse.

Two implementation notes worth keeping:

- **One condition, one define.** The natural shape is a subject prefix plus a body banner —
  the same condition written twice, held in lockstep by comment alone. Go text/template
  cannot return a boolean from a template and cannot capture another template's output into
  a variable, so the second cannot be derived from the first. A drifted pair marks the
  subject and not the body: the half-marked failure the file exists to prevent. One define,
  both consumers prefix it.
- **Match each label value WHOLE, not a joined string.** `join " " .CommonLabels.Values`
  lets two adjacent labels manufacture a `probe201a` token (Grafana supplies
  `grafana_folder`, and folder names may contain a space). A false positive stamps `[DRILL]`
  on a REAL incident and invites the operator to ignore it — the dangerous direction.

### Announce before firing; the window var is the machine-checkable half

Any drill that can reach a live channel is announced to the operator **first**. For
agent-driven runs the machine-checkable half is a deploy-time consent token:

```bash
ansible-playbook playbooks/deploy-services.yml --limit eq12_docker \
  --tags observability -e observability_drill_issue=<issue#>
```

The guard `Enforce the operator drill window on drill-marked alert rules (#201)` in
`roles/services/observability/tasks/main.yml` fails the deploy unless every drill-marked
rule carries **both** markers, agreeing with that number. An agent cannot page a human
channel with a synthetic alert unless a human typed the issue number.

A **half-marked** rule fails even with the window open. That is not pedantry: the label
without the uid is a drill nobody can grep for afterwards, and the uid without the label is
a drill that pages UNMARKED — the original defect. A guard that accepted either alone would
accept exactly what it exists to stop.

The window value's SHAPE is validated (digits only) because it is interpolated into the
uid-prefix regex. Rehearsed: with `-e observability_drill_issue='.*'` the guard's own message
reads `UIDs not prefixed obs-drill-.*-: none` — the regex alone would have admitted it. A
bound that fails open when its own input is unvalidated is not a bound.

## The graded mechanism table

Every drill mechanism this batch used or considered, graded by what it can and cannot prove.
Pick the highest grade that answers your question — and note that only one of them proves
delivery at all.

| Mechanism | Grade | Verdict |
| --- | --- | --- |
| **Pending-only staleness probe** — push for less than `for:` − staleness, `noDataState` ≠ Alerting, probe-named labels (#217) | **A**, for labelling only | Structurally cannot page: the series is gone before `for:` elapses, so the instance can reach `Pending` (where its labels are readable) and never `Alerting`. The safety is arithmetic, not vigilance. Proves instance labels; proves **nothing** about delivery. See [pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md](pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md). |
| **Unfireable fixture rule** — `noDataState: OK` + `execErrState: OK` + a long `for:` (#212) | **A**, for fixtures | Three independent reasons it cannot page. For a rule that must EXIST and EVALUATE (to exercise a served-set or freshness guard) but must never fire. |
| **Live delivery drill** — real rule, real routing, `[DRILL]`-marked, operator pre-announced, bounded self-resolving probe series (#188, #201) | **A with consent** | The ONLY mechanism that proves delivery. Everything in this document exists to make it safe rather than to avoid it. |
| **Dead-webhook contact point + a policy route matched on a dedicated fixture label** (#181) | **B** | Proves routing without delivering anything. But Grafana routes are FIRST-MATCH-WINS with no `continue`: a mis-ordered or loosely-matched route black-holes REAL alerts. Only with an equality matcher on a dedicated label, and verified by live policy readback. |
| **A scoped Grafana silence over a real firing rule** | **C** | Works, then expires SILENTLY — an expired silence suppresses nothing, and #183 found its own silence had expired. Only with expiry ≥ 2× the drill window, `state=active` verified BEFORE and AFTER, and it defeats delivery-proof by construction (you silenced the thing you were trying to observe). |
| **`isPaused: true`** | **F** | Measured in #212: a paused rule is excluded from evaluation entirely. A drill that must evaluate cannot use it, and it proves nothing about a rule that does. |

## Leak detection and retirement

A drill that outlives its window is caught in **two independent places**, which is why
neither has to be perfect:

- **Repo-side** — the window guard above. Master never carries drill rules (they live on a
  branch and are retired before merge), so the next routine deploy passes no window var,
  finds the rule still in the files, and fails before shipping anything.
- **Served-side** — #212's set-equality check. A drill rule left in Grafana after its branch
  died is a uid **NOT IN THE REPO** and fails the next deploy from master. This is the #220
  leak class, and it is not hypothetical: **deleting the file does NOT delete the rule.**
  Grafana's file provisioner reconciles the rules a file DECLARES, never the ones it stopped
  declaring — measured on #212's first live run, with `delete: true` on the rsync and a
  Grafana restart, three dropped rules still served afterwards.

**Retirement is a `deleteRules:` block, not a file deletion:**

```yaml
deleteRules:
  - orgId: 1
    uid: obs-drill-201-scratch
```

`deleteRules:` is a TOP-LEVEL key, so a tombstone-only file has no `groups:` and trips the
per-file "contributed rules" assert — put the block in a file that also ships rules.

**The provisioning API cannot do it instead, and this was measured in #212, not here.**
#201 inherits the finding; it did not re-derive it. The measurements live in master's
served-set `fail_msg` (`roles/services/observability/tasks/main.yml`, commit `ae9ce5e`):
`DELETE /api/v1/provisioning/alert-rules/<uid>` on a file-provisioned rule returns HTTP 409
`cannot delete with provided provenance '', needs 'classic-file-provisioning'`, and
`X-Disable-Provenance: true` does not help — it is what sets the provenance to `''` and
CAUSES the mismatch. That header path works only on an API-CREATED stray. The same #212 run
measured the sibling fact this section opens with: rules dropped from the files were still
served afterwards, with `delete: true` on the rsync and a Grafana restart.

### The same leak, one level over: CONTACT POINTS and silences

Measured on the live stack 2026-08-30, immediately after this issue's own apply, and it is
the reason this section is not rules-only.

**A drill contact point leaks exactly like a drill rule, and nothing catches it.** #181 used
the grade-B dead-webhook mechanism. Its receiver was still there:

```
uid='issue-181-sink-webhook'  name='issue-181-sink'  type=webhook  provenance='file'
```

`provenance='file'` — so the stanza had been removed from the template and Grafana kept the
object anyway, the #220 mechanism one level over from rules. #212's set-equality guard
compares RULES only, so nothing failed. It is inert today (the live policy tree references
`homelab-email`, `homelab-telegram` and `homelab-critical` and nothing else, so no route
points at it), which is the only reason it is a finding rather than an incident: a webhook
receiver that a route DID match would black-hole every alert taking that route.

Retirement is `deleteContactPoints: [{orgId: 1, uid: <uid>}]`, the documented sibling of
`deleteRules:` — **not exercised on this stack, and it must not be tried casually.** A
provisioning key this repo has never run is a boot-fatal gamble if the schema is wrong
(a bad alerting document withdraws every rule), and the receiver is inert, so #201
deliberately left it in place and wrote it down instead of cleaning it up inside an
unrelated deploy window. Retire it in a change that can watch Grafana come back.

**The grade-C hazard is not theoretical either.** Both silences on the stack were expired:

```
630377e6-…  expired  endsAt 2026-08-29T23:48:36Z
af6d6453-…  expired  endsAt 2026-08-29T22:45:06Z
```

An expired silence suppresses nothing and says so nowhere. Anything relying on one to stay
safe was unprotected from the moment it lapsed.

**So the retirement checklist is per-OBJECT, not per-rule:** rules via `deleteRules:`,
contact points via `deleteContactPoints:`, routes by removing them from the policy template
(the tree is fully rewritten each deploy, so routes do NOT leak), silences by expiry —
and re-read each object class from the API afterwards, because for rules and contact points
alike, removing the declaration does not remove the object.

For a drill SERIES there is nothing to retire: it self-resolves when the datasource's
staleness window empties. That is a property to preserve deliberately, not a happy accident
— never push a synthetic series without a bound on how long you will push it.

## What the marker does NOT license

- **It does not move drills off the real path.** Part of what #188's probe proved is that
  the REAL routing works end to end. A drill routed to a side channel stops testing the
  thing that matters. The marker makes the real path safe to use; it does not authorise
  avoiding it.
- **It does not replace the announcement.** `-e observability_drill_issue=<n>` proves a
  human typed a number, not that a human is awake and expecting a phone to buzz.
- **It does not make green a proof.** Delivery is still counted Alertmanager-side —
  Grafana's own notification counters per integration, before and after — never inferred
  from a playbook recap. "Configured is not delivering" (#174) applies to drills too.
- **It is not a licence to weaken verification.** #201's own issue text is explicit: do not
  "fix" this by verifying against config diffs or dry-runs instead. The verification method
  was right; its packaging and its consent model were wrong.

## Verifying the templates without sending anything

Grafana renders a candidate notification template against fixture alerts, persisting and
sending nothing:

```
POST /api/alertmanager/grafana/config/api/v1/templates/test
{"name": "<any>", "template": "<the full template text>", "alerts": [ ... ]}
```

Measured on Grafana 13.2.0: the provisioned-template list read `[]` before and after every
call. Use it as the pre-apply proof, because **a broken alerting provisioning document is
boot-fatal and withdraws every provisioned rule** — and re-run it after any Grafana major
bump, since it is also what confirms the functions exist. Confirmed present on 13.2.0:
`match` (pattern first), `join` (separator first — Alertmanager redefines `strings.Join`
that way), `.CommonLabels.Values`, and the builtins `default.title` / `default.message`.

The four fixtures to keep re-running:

| fixture | rendered subject |
| --- | --- |
| `drill="201"`, firing | `[DRILL #201] [FIRING:1] ...` |
| `devname="probe201a"`, firing | `[DRILL] [FIRING:1] ...` |
| `devname="sdd"`, firing | `[FIRING:1] ...` (no marker) |
| `drill="201"`, resolved | `[DRILL #201] [RESOLVED] ...` |

The reference is resolved at NOTIFY time, not at provisioning time, so a renamed define or a
deleted template file fails only when an alert actually needs sending. That pairing is
therefore asserted statically at deploy time (`Verify every notification template the contact
points reference is defined`), matching the full call form `template "<name>" . }}` so that
neither file's prose can vote a name into the reference set.

## See also

- [pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md](pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md)
  — the grade-A labelling mechanism, and its preconditions.
- [prove-notification-delivery-not-just-config-validity.md](prove-notification-delivery-not-just-config-validity.md)
  — why a delivery drill is needed at all.
- [grafana-alert-panelid-pairing-breaks-all-provisioned-rules.md](../integration-issues/grafana-alert-panelid-pairing-breaks-all-provisioned-rules.md)
  — the boot-fatal provisioning class this file's rollback plan assumes.
- [experiment-must-discriminate-between-hypotheses.md](experiment-must-discriminate-between-hypotheses.md)
  — including the unvalidated-input-makes-a-bound-inert lesson the window var's shape check
  implements.

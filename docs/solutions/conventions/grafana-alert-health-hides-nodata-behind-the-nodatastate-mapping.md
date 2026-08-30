---
title: "Grafana derives a rule's `health` AFTER the noDataState mapping, so a NoData rule reports `health: ok`"
date: 2026-08-29
category: conventions
module: services/observability
problem_type: convention
component: monitoring
severity: high
applies_when:
  - "writing a deploy-time or CI guard that asserts a Grafana alert rule is healthy"
  - "a rule queries a metric name, label, or series that may have been renamed, removed, or typo'd"
  - "choosing between rule `health`, rule `state`, and per-instance `alerts[].state` as the signal an automated check reads"
  - "proving a new guard can actually detect the defect it claims to detect"
  - "measuring a would-page alert state on the live stack without notifying the operator"
symptoms:
  - "`health: ok` with a fresh `lastEvaluation` on a rule whose query returns no series at all"
  - "A deploy-time guard asserting `health` is `ok` passes against the exact defect it was written to catch"
  - "The same broken query reads `state: inactive` under noDataState=OK and `state: pending` under noDataState=Alerting"
  - "A renamed label or removed metric produces no alert, no error, and no degraded health anywhere in the rule API"
related_components:
  - grafana
  - victoriametrics
  - alertmanager
  - services/_deploy
tags:
  - grafana
  - alerting
  - nodata
  - verification
  - silent-failure
  - guard
  - observability
  - negative-test
---

# A Grafana alert rule's `health` describes the MAPPED state, so a rule whose query returns nothing reports `health: ok`

## Context

A deploy guard in `services/observability` asserted, per rule, that Grafana reported
`health == "ok"` with a recent `lastEvaluation`, and was described in the role as proof
that the provisioned rules were "evaluating healthily". The natural reading of that
assertion is: *if a rule's `expr` names a metric that no longer exists, this deploy
fails.*

It does not. The guard was not audited by inspection — it was pointed at the exact defect
it claimed to detect, and it **passed the broken rule**. That pass is the only reason
anyone discovered the asserted field cannot carry the fault. Everything else about the
guard was correct: the endpoint, the retry budget, the subset-not-count shape, the
RFC3339 parse. The field was wrong, and a wrong field is invisible to code review, to
`ansible-lint`, and to a green apply.

Measured 2026-08-29 on `eq12_docker`, Grafana 13.2.0, during #212.

## Guidance

### The rule: `health` is derived AFTER `noDataState` is applied

Grafana evaluates the query, maps a NoData result through the rule's `noDataState`
setting, and only then derives the rule's `health`. `health` therefore describes the
**mapped** state, never the raw evaluation result. A query that returns nothing — a
typo'd metric name, a renamed label, a series that legitimately stopped existing — is not
an unhealthy evaluation as far as this field is concerned. It is a successful evaluation
of a rule whose result was mapped to whatever the author chose.

Both cohorts were measured directly, one mapping at a time. The `Alerting` cohort ran
under a scoped Alertmanager silence so it could not page:

| `noDataState` | `rule.health` | `rule.state` | `alerts[].state` | delivered? |
| --- | --- | --- | --- | --- |
| `OK` | `ok` | `inactive` | `Normal (NoData)` | no |
| `Alerting` | `ok` | `pending` | `Pending (NoData)` | no (`/api/v2/alerts` showed `fixture_present=0`) |

`health` is `ok` in **both**. This is not an inference from the fleet's mapping counts —
this repo declares 36 rules, 21 `Alerting` and 15 `OK`, plus, during this measurement, one
temporary scratch fixture. None set `NoData`, which is therefore the one mapping this repo
does not ship and the only one not measured here. Each row above is what the API returned
for that mapping in turn. The broken rule also reported `totals={normal: 1}` and
a fresh `lastEvaluation`, so every field the guard read looked exactly like health.

### Control the query at the datasource, not at the rule

"The rule looks broken" is not evidence that the query returned nothing; a rule can be
mute for reasons that have nothing to do with its data. Ask the datasource directly, in
more than one form, because a wrapper function can manufacture or suppress a result on
its own:

```
sum(increase(selftest_212_no_such_metric[15m]))  -> resultLen=0
selftest_212_no_such_metric                      -> resultLen=0
increase(selftest_212_no_such_metric[15m])       -> resultLen=0
```

All three empty is what makes "the query genuinely returned nothing" a fact rather than a
reading of the rule's own opinion of itself.

### The honest signal is per-instance, and it is mapping-independent

At `GET /api/prometheus/grafana/api/v1/rules`, the per-instance `alerts[].state` carries a
`(NoData)` suffix in **both** rows of the table above. That is the field a real NoData
detector must be built on. `health` cannot see absence-of-data for any rule under any
mapping this repo ships; `state` cannot either, for an independent reason — `inactive` is
also what a rule that has **never run** reports, so `state` cannot separate "evaluated,
healthy" from "never evaluated".

### Fixture-design facts for NoData instances

Each of these cost real time to establish, and a wrong assumption about any one of them
silently breaks the test rather than failing it:

- **NoData-born instances INHERIT the rule's custom labels.** This is what makes a silence
  matcher on a custom label actually cover an instance that does not exist yet.
- **Their `alertname` is the RULE TITLE, not `DatasourceNoData`.** Older Grafana stamped
  the latter; a matcher written from memory of that behaviour silences nothing.
- **They gain `datasource_uid` and `ref_id` labels** identifying which query returned
  nothing — useful for attribution when a rule has several.
- **`for:` IS honoured on a NoData→Alerting transition.** The instance stopped at
  `Pending`. So `for:` is real belt — but the silence was the control, and belt is not
  control.
- **A rule that has never evaluated reports `lastEvaluation: 0001-01-01T00:00:00Z`**, the
  zero time, which a recency check correctly reads as ancient. That much of a freshness
  guard does work.

### Safety procedure for measuring a state that would page

Generalise this; it is reusable for any deliberate transition into a notifying state.

1. **Create the silence FIRST**, before the rule that could transition exists. A silence
   created after the fixture is a race with the scheduler.
2. **Verify `state=active`, not merely that the silence exists.** A sibling drill's
   silence was found silently flipped to `expired`. Read the state field.
3. **Match on a DEDICATED unique label** (`selftest_212="true"`), never on a shared value
   like `component=selftest` — a broad matcher can silence a real rule by accident, and
   nothing will tell you it did.
4. **Prove non-delivery FROM THE DELIVERY SIDE**: `GET
   /api/alertmanager/grafana/api/v2/alerts` showing the fixture absent. The existence of a
   silence is not proof that nothing was delivered.
5. **Tear down in order and confirm**: delete the fixture, expire the silence, then LIST
   silences and confirm none is left active. A forgotten silence masking real alerts is
   strictly worse than the detection gap being tested.

### One thing suspected and NOT tested

`execErrState` plausibly hides `health: error` by the identical mapping — same shape, same
place in the pipeline. **Nobody has tested it.** Do not write it down as known, and do not
let a guard lean on `health: error` on the strength of this paragraph.

## Why This Matters

The repo already carries the rule that *a guard you have not seen fail is not a guard*,
learned from `failed_when: false` assigning `failed: False` and turning a paired assert
into `assert: true`. This finding extends it in a direction that inspection cannot reach.
That earlier case was a guard whose **logic** was vacuous. This one is a guard whose logic
is sound, whose extraction is deliberately non-vacuous (a subset test over a set of uids
that each passed a per-rule predicate, precisely so an empty parse cannot pass), whose
endpoint and retry budget are right — and which still cannot see the fault, because the
**field it asserts on is downstream of the mapping that erases the fault**.

Nothing repo-side can catch that. The YAML is valid, the Jinja is clean, `--check` skips
the task because its input is a live Grafana that only exists after a real apply, and
every real run is green. Watching the guard fail is not polish applied after the guard
works; it is the step that tells you whether the field you chose can represent the defect
at all. **A guard that passes its own negative test is reporting a fact about the guard,
not about the system.**

The cost of skipping that step here would have been a permanent false sense of coverage
over the exact failure the observability role exists to catch: an alert rule that
provisions perfectly, loads, evaluates on schedule, reports healthy forever, and watches
nothing — and, under `noDataState: Alerting`, one that will eventually page permanently
for a reason its own health field denies.

## When to Apply

- **Before trusting any deploy-time or CI guard, point it at the defect it names and
  watch what it reports.** If it passes, the finding is about the guard. This is
  mandatory when the guard reads a *derived* or *summary* field — `health`, `status`,
  `state`, `ok`, an exit code — rather than the raw observation.
- **Whenever an assertion reads a field whose value passes through a user-configurable
  mapping.** `noDataState` and `execErrState` are the Grafana instances; the general shape
  is any setting that says "when X happens, report Y instead". A field downstream of such
  a setting describes the operator's preference, not the world.
- **Whenever a guard's documentation and its predicate disagree in scope.** "Is evaluating
  healthily" and `health == "ok"` read as the same claim and are not. Re-scope the prose
  to what the predicate actually proves, and say out loud what it does not.
- **Whenever a measurement requires entering a state that notifies.** Use the five-step
  silence procedure above rather than relying on `for:` headroom, timing, or care.
- **Whenever a per-rule fact is inferred from fleet-wide counts.** "No rule here uses
  `noDataState: NoData`, so this cannot happen" is not a measurement of any rule; measure
  each mapping in turn. Counts drift, and a count taken while a test fixture is live is
  not the fleet's count at all.

## Examples

**The negative test that produced the finding.** A scratch rule was provisioned whose
`expr` named `selftest_212_no_such_metric` — a metric that does not exist — with the
datasource control above run first to establish that the query really returned nothing.
The live rule then reported:

```
health         = ok
state          = inactive          (noDataState: OK)  /  pending  (noDataState: Alerting)
totals         = {normal: 1}
lastEvaluation = fresh
alerts[].state = Normal (NoData)   /  Pending (NoData)
```

and the deploy guard passed it. The `(NoData)` suffix on `alerts[].state` is the only
field in that block that changed with the fault.

**What the guard was re-scoped to, and what it deliberately no longer claims.** The second
half of `Verify Grafana serves and evaluates exactly the provisioned alert rules`
(`ansible/roles/services/observability/tasks/main.yml:1700`, asserting at `:1953`) is now
an **evaluation freshness** guard: it proves every unpaused rule is still being RUN,
catching a stalled scheduler, a wedged group, or a rule that silently stopped evaluating.
It explicitly does **not** catch a typo'd selector. The `health == "ok"` clause stays — it
costs one comparison and it fires if Grafana ever does report a non-ok health — but it is
no longer load-bearing, and the role says so both in the note above the task and in the
assert's own `fail_msg`, rather than only in a commit message. The `fail_msg` is the part
that matters: it is what an operator reads at 3am, and it previously told them this guard
fails the deploy on a typo'd selector — the exact claim this finding falsified.

**The distinction to keep when reading any similar guard.** Ask of each asserted field:
*what value would this field hold if the fault were present?* If the answer is "the same
one it holds now", the guard is decoration regardless of how carefully the rest of it is
built.

## Related

- CLAUDE.md, *"A guard you have not seen fail is not a guard"* — the rule this extends.
  That entry covers guards whose logic cannot fail; this one covers a guard whose logic is
  sound and whose **field** cannot carry the fault.
- [verification-instrument-must-distinguish-fixed-from-broken.md](verification-instrument-must-distinguish-fixed-from-broken.md)
  — the same discipline applied to a verification's instrument rather than to a deploy
  guard's predicate.
- [experiment-must-discriminate-between-hypotheses.md](experiment-must-discriminate-between-hypotheses.md)
  — where "state the falsifier before measuring" comes from; here the falsifier was "the
  guard fails the broken rule", and it did not.
- [pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md](pending-only-synthetic-probe-verifies-alert-labelling-without-paging.md)
  — the arithmetic-safety sibling of the silence procedure above, for probes that must not
  reach `Alerting`.
- #212 — the guard whose scope this corrected. #232 — the tracked detection gap, with a
  fix-spec built on `alerts[].state`. #220 — a provisioning-lifecycle gap found in the same
  verification. #201 — the drill-safety and testability work that would make this class of
  test routinely safe rather than hand-built.

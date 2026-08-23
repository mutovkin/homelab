---
title: "A provisioned Grafana datasource is frozen unless its `version:` increases: the rotated secret reached the container but never the application"
date: 2026-08-22
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "every VictoriaMetrics-backed Grafana alert rule fails evaluation with `[sse.dataQueryError] failed to execute query [A]: got unexpected response status code: 401`"
  - "the operator is paged continuously for ~15 minutes, including by the rule whose only job is to report that alert delivery has gone blind"
  - "the `.env` on disk, the container env, an in-container `curl -u` and Grafana's `/api/health` ALL pass while the datasource is broken"
  - "the failure follows a credential rotation and nothing in the deploy reports it"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - grafana
  - victoriametrics
  - victorialogs
  - ansible
  - docker-compose
tags:
  - grafana
  - provisioning
  - datasource
  - credential-rotation
  - observability
  - ansible
  - silent-green
---

# A provisioned Grafana datasource is frozen unless its `version:` increases: the rotated secret reached the container but never the application

## Problem

PR #165 rotated `vault_vm_auth_password`, the password VictoriaMetrics enforces via
`--httpAuth`. Every consumer reads it from the one Ansible-templated `.env`, so the
rotation propagated correctly to VictoriaMetrics, vector, telegraf and the Grafana
*container*. It never reached the Grafana *application*: `version:` was pinned at `1`
in the provisioned datasource file, and Grafana re-applies a provisioned datasource
only when that number increases. Grafana kept querying with the old secret, every
VictoriaMetrics-backed alert rule 401'd for roughly 15 minutes and paged continuously,
and the rule that exists to announce exactly this class of outage was one of the rules
that was down.

## Symptoms

- Every VictoriaMetrics-backed alert rule failed evaluation. Verbatim:

  ```
  [sse.dataQueryError] failed to execute query [A]: got unexpected response status code: 401
  with request url: "http://victoriametrics:8428/api/v1/query?query=min%28lag%28grafana_alerting_alertmanager_receivers%5B24h%5D%29%29&step=5m0s&time=1787436110"
  ```

- Continuous paging for ~15 minutes, on a rule set that is otherwise quiet: on a
  healthy stack 13 of the 14 rules are inactive and the fourteenth is a `vector(1)`
  delivery heartbeat that is *designed* to fire.

- **The compounding symptom, and the reason this is worth a document.** The failing
  set included `obs-alert-delivery-telemetry-absent`
  (`ansible/roles/services/observability/files/data/grafana/provisioning/alerting/delivery-health.yaml:307`),
  whose entire job is to report that alert delivery has gone blind. Its query
  (`min(lag(grafana_alerting_alertmanager_receivers[24h]))`) runs against the very
  datasource that was 401'ing. The alerting stack could not report its own outage
  because the reporter was the thing that was out. A watchdog that shares a
  dependency with what it watches is not a watchdog for that dependency.

- Nothing in the deploy noticed. The role restarts Grafana on config change, gates on
  `/api/health`, asserts the containers survive a settle period
  (`ansible/roles/services/observability/tasks/main.yml:383`, `:403`, `:417`) — all of
  which passed while every rule failed.

## What Didn't Work

Every process-level check available reported health. This table is the actual
investigation, in the order the checks were run:

| Check | Reported | Why it was misleading |
| --- | --- | --- |
| `.env` on disk | new password | `env.j2:23` is `VM_AUTH_PASSWORD='{{ vault_vm_auth_password }}'` — the template's job ends at the file. It proves Ansible rendered the rotation, nothing about who read it. |
| grafana container env `VM_AUTH_PASSWORD` (sha256 vs the vault value) | **match** | `compose.yaml:320` passes `VM_AUTH_PASSWORD: ${VM_AUTH_PASSWORD:-}` into the grafana container. The variable was correct *inside the process*. Grafana only reads it at provisioning time, and provisioning declined to run. |
| `curl -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" …:8428/api/v1/query` from INSIDE the grafana container | `200` | This is the strongest-looking check and the most misleading one. It proves the network path, the VictoriaMetrics credential, and the env var are all correct — using the credential *from the environment*. Grafana does not query from the environment; it queries from `secureJsonData` stored in its own database. The check bypassed the exact component that was broken. |
| Grafana `/api/health` | `200` | Proves the process started and that provisioning **parsed**. Parsing is not applying. A frozen datasource parses perfectly. |
| Grafana's **stored** datasource (`/api/datasources/uid/<uid>/health`) | old password → **401** | The first check that touched the application's own state rather than the process's environment. This is the check that found it, and it is the one now wired into the deploy. |

The pattern: four checks interrogated the *delivery of the secret* and one
interrogated the *use of the secret*. Only the last one can see this failure, because
the secret was delivered correctly — it was never consumed.

**A second, separate false start worth recording: the too-short verification window.**
An early post-fix measurement reported "0 × 401 in the last 45 seconds" and was treated
as evidence. It was not. Grafana rules evaluate on an interval, so a 45-second window
can contain very few evaluations of any given rule — and this window also straddled the
container restart, mixing pre-fix and post-fix time. Absence of errors in a window
shorter than the thing you are measuring is not evidence of repair; it is a sampling
artifact. It was re-measured against an absolute timestamp anchored strictly after the
restart, over a window long enough to contain multiple evaluation cycles of every rule.
This is the same category error CLAUDE.md records for log-gap statistics
(`docs/solutions/conventions/absence-alerts-need-a-continuously-exported-sentinel.md`):
absence of incidental traffic is not evidence of anything until you have measured how
long the window has to be before absence means something.

**Why the pin survived so long (session history).** A scan of this repo's prior agent
sessions over the preceding week found **zero** occurrences of `secureJsonData`,
`basicAuth`, `datasources.yaml`, `httpAuth`, `401`, or `version: 1`. Every Grafana
provisioning change in that window touched the *alerting* tree
(`provisioning/alerting/*.yaml`) or the SMTP contact point — never the datasource
document. The datasource was applied once at creation and never edited, re-read or
verified again, which is exactly the condition under which a version gate is
invisible: the gate only bites when the file changes, and nobody had changed it.

Two details from those sessions sharpen the lesson rather than softening it
(session history):

- **The correct rule was already in place and still could not see this.** During an
  earlier `isPaused` incident — where a provisioning file was believed applied but the
  on-host copy was two days stale and both rules were still unpaused — the ruling
  recorded was *"confirming from Grafana's API rather than trusting the file
  arriving"*. That is the right instinct, and it is why the alerting side of this role
  is verified through the API. But those API checks authenticated by sourcing the
  deployed `.env` on the host (`ssh … 'set -a; . /data/deploy/observability/.env;
  set +a; curl …'`). A stale in-Grafana secret is invisible to that pattern **by
  construction**: it proves the credential in `.env` works against the backend, which
  is the one thing that was never in doubt. The rule was right; the instrument it was
  implemented with shared the blind spot.
- **The mirror-image risk had been flagged; this one had not.** A reviewer had raised
  that "Grafana could restart into a crash loop from a bad provisioning document with
  the run reporting converged" — a *syntactically invalid* document, which
  `/api/health` does catch, and which is why that gate exists. The inverse case — a
  perfectly valid document that Grafana silently declines to re-apply — was never
  considered. Both are "provisioning did not take"; only one of them is loud.
- **Rotation had been thought about, but as a different hazard.** A 2026-08-20
  measurement established this password is 64 alphanumeric characters and warned that
  a future rotation to a value containing a backtick would be a root code-execution
  path through a shell-sourced env file. Rotation as a *re-provisioning* hazard —
  which consumers must be re-pointed at the new value — was never raised.

## Solution

Fixed in PR #166, merged to `master`.

**1. Bump `version:`, and say at the field what the field is for.**

Before:

```yaml
    editable: true                                      # Allow editing in UI
    version: 1                                          # Datasource version
```

After
(`ansible/roles/services/observability/files/data/grafana/provisioning/datasources/datasources.yaml:21-30`,
and identically at `:49-58` for VictoriaLogs):

```yaml
    editable: true                                      # Allow editing in UI
    # VERSION IS THE UPDATE GATE, NOT A LABEL — BUMP IT WHENEVER ANY FIELD BELOW
    # CHANGES, ESPECIALLY THE PASSWORD. Grafana's provisioner only re-applies a
    # datasource when this number is greater than the stored one; with it pinned,
    # an existing datasource is NEVER updated again, secureJsonData included. That
    # is not theoretical: rotating vault_vm_auth_password (#165) left Grafana
    # querying with the OLD secret while its container env, the .env file and an
    # in-container curl all showed the NEW one — every check passed and every
    # VM-backed alert rule 401'd. See the datasource-health assert in
    # tasks/main.yml, which now turns that silence into a failed deploy.
    version: 2
```

The comment `# Datasource version` was accurate and useless: it described the field
as a label. The replacement describes it as the gate it actually is, at the point of
use, with the incident that proves it. Both datasources were bumped even though only
the metrics one had broken — the logs datasource carried the identical freeze.

**2. Assert at deploy time that each provisioned datasource can REACH its backend.**

`ansible/roles/services/observability/tasks/main.yml:559-585`:

```yaml
- name: Check every provisioned Grafana datasource can actually reach its backend
  vars:
    _declared: >-
      {{ (lookup('file', role_path ~ '/files/data/grafana/provisioning/datasources/datasources.yaml')
          | from_yaml).datasources }}
  ansible.builtin.uri:
    url: "http://127.0.0.1:3000/api/datasources/uid/{{ item.uid }}/health"
    user: "{{ vault_grafana_user }}"
    password: "{{ vault_grafana_password }}"
    force_basic_auth: true
    headers:
      Host: "{{ vault_grafana_domain }}"
    return_content: true
  register: grafana_datasource_health
  # The datasource plugins finish loading slightly after /api/health goes 200.
  until: grafana_datasource_health.json.status | default('') == 'OK'
  retries: 6
  delay: 5
  # ignore_errors, NOT failed_when: false — the assert below must be able to read
  # `failed`, and `failed_when: false` ASSIGNS failed=False, which would make the
  # assert vacuously true. See the "a guard you have not seen fail is not a
  # guard" gotcha in CLAUDE.md.
  ignore_errors: true
  loop: "{{ _declared }}"
  loop_control:
    label: "{{ item.name }} ({{ item.uid }})"
  when: not ansible_check_mode
```

Three details in that task are load-bearing:

- **The uids are read FROM the provisioning file** (`lookup('file', …) | from_yaml`),
  not listed in the task. A datasource added to version control is therefore checked
  automatically; there is no second list to forget to update. The uids are already
  pinned in the file as identities (`datasources.yaml:15`, `:43`) because alert rules
  reference datasources by uid — this reuses that pin rather than adding a parallel one.
- **`Host:` header, not a redirect follow.** `GF_SERVER_DOMAIN`/`ROOT_URL` make Grafana
  301 any request whose Host does not match, and following that redirect would leave
  the container and end up testing the reverse proxy's public path instead of this
  Grafana.
- **`until` + retries.** The datasource plugins finish loading slightly after
  `/api/health` returns 200, so a single immediate probe races startup.

`ansible/roles/services/observability/tasks/main.yml:587-606` turns it into a verdict:

```yaml
- name: Assert every provisioned Grafana datasource is healthy
  ansible.builtin.assert:
    that:
      - item is not failed
      - item.json.status | default('') == 'OK'
    fail_msg: >-
      Grafana datasource {{ item.item.name }} ({{ item.item.uid }}) is NOT
      healthy: {{ item.json.message | default(item.msg | default('no response')) }}.
      Grafana is running and provisioning parsed, so nothing else in this play
      will notice — but every alert rule querying this datasource is now failing,
      which means the alerting stack cannot report its own outage. If this
      followed a credential rotation, the cause is almost certainly the `version:`
      field in
      files/data/grafana/provisioning/datasources/datasources.yaml: Grafana
      re-applies a provisioned datasource only when that number INCREASES, so the
      new secret was never written. Bump it and redeploy.
    quiet: true
  loop: "{{ grafana_datasource_health.results | default([]) }}"
  loop_control:
    label: "{{ item.item.name }}"
  when: not ansible_check_mode
```

The `fail_msg` names the most likely cause and the fix. The next person to hit this
gets the answer in the failure, not a 401 and a fifteen-minute search.

**3. The guard was verified against the live defect before being trusted** — and
without re-paging the operator, by pointing a throwaway datasource at the same backend
with a deliberately wrong password:

| Datasource | `/api/datasources/uid/<uid>/health` | Guard |
| --- | --- | --- |
| throwaway, deliberately wrong password | `status=ERROR`, `got response code 401` | **fires** |
| real VictoriaMetrics | `status=OK` | passes |

The throwaway was deleted afterward. Both legs matter: a guard that only ever passes
has not been shown to be capable of failing, and a guard that fires on everything is
not a signal either.

**Post-fix verification** (against a window anchored strictly after the restart, not a
45-second sample): 0 × 401 and 0 failed evaluations; metrics datasource 4/4
`status=ok`; both datasources reporting `"Data source is working"`; all 14 alert rules
healthy — 13 inactive plus the `vector(1)` delivery heartbeat that is designed to fire.

## Why This Works

**Grafana's provisioner is a version-gated upsert, not a desired-state reconciler.**
On startup it reads the provisioning files and, for each datasource, compares the
file's `version` against the version stored in its own database. It applies the file
only when the file's number is **greater**. Equal or lower is a no-op — silently, and
including `secureJsonData`, which is where `basicAuthPassword` lives. This is not a
quirk of secrets handling; the whole record is frozen. With `version: 1` pinned in
version control, the file's number can never exceed the stored number after the first
successful apply, so the datasource was applied exactly once in its life and then
became immutable from the repo's point of view. Editing any field — url, auth user,
timeouts, password — changed nothing on the host, forever.

That is what makes this failure mode uniquely nasty: the file in version control is
not a description of the running state, it is a description of the state at first
apply. Every other file this role deploys is content-addressed by rsync and takes
effect on the next restart. This one has a hidden generation counter that the deploy
pipeline knows nothing about.

**Why `/api/datasources/uid/<uid>/health` is the right probe and `/api/health` is not.**
They answer different questions:

- `/api/health` is unauthenticated and returns 200 once Grafana has started and
  finished reading provisioning. It is a *liveness* check, and it is genuinely useful
  for what the role already used it for: Grafana treats a rejected provisioning
  document as a fatal startup error, so a mistyped alert rule crash-loops it and
  `/api/health` catches that (`ansible/roles/services/observability/tasks/main.yml:377-393`). What it cannot distinguish is
  "provisioning parsed and applied" from "provisioning parsed and was skipped by the
  version gate." Both are 200.
- `/api/datasources/uid/<uid>/health` makes Grafana issue a real query to the backend
  **using the stored credential**, and returns `status: OK` or `status: ERROR` with the
  backend's message (here, literally `got response code 401`). It is a *reachability*
  check, and reachability is the property alert rules actually depend on.

The distinction generalizes: liveness checks interrogate the process, reachability
checks interrogate the dependency the process exists to use. Every check that passed
during this incident was a liveness or delivery check. The one class of check that
could see the failure was the one that made the application use the thing under test.

**Why `ignore_errors`, not `failed_when: false`.** `failed_when: false` does not "let
the result through" — it **assigns** `failed: False` on the registered result. A paired
`assert: - item is not failed` then evaluates `assert: true` and can never fire.
`ignore_errors: true` lets the task's real failure state survive into the register, so
the assert can read it. CLAUDE.md records this measured against a `wait_for` that timed
out: under `failed_when: false` the result carried `failed=False` and the assert passed;
under `ignore_errors: true` it carried `failed=True` and the assert fired. The
distinction is only visible when the guarded thing actually breaks, which is why a
broken guard of this shape can sit inert for a deployment's whole life while reporting
green — one in this repo did.

Note that `failed_when: false` is not wrong everywhere: the VictoriaLogs ingest probe
40 lines above (`ansible/roles/services/observability/tasks/main.yml:511`) uses it deliberately, because its assert reads the
*response content* (`rows` count), never `failed`. The rule is not "never use
`failed_when: false`" — it is "if an assert reads `failed`, the task above it must be
`ignore_errors`."

## Prevention

- **Bump `version:` in the same change as any credential — or any other datasource
  field.** Treat the number as part of the edit, not as metadata. The comment now sits
  directly above the field in both datasources
  (`datasources.yaml:21-30`, `:49-58`) precisely so it is unmissable at the moment of
  editing. A rotation PR that touches `vault_vm_auth_password` and does not touch
  `datasources.yaml` is incomplete by construction.

- **Assert reachability, not liveness.** For anything where the process holds a stored
  copy of a credential or endpoint, add a deploy-time check that makes the application
  *use* it. Liveness (`/api/health`, `docker inspect`, "the container is Running") tells
  you the process survived; it says nothing about whether the process is doing its job.
  Read the identities to check out of the same file that declares them, so the check
  cannot fall behind the config:

  ```yaml
  vars:
    _declared: >-
      {{ (lookup('file', role_path ~ '/files/data/grafana/provisioning/datasources/datasources.yaml')
          | from_yaml).datasources }}
  ```

- **Verify a new guard against the live defect before trusting it, and do it without
  collateral damage.** Reproduce the broken state in a disposable object — here, a
  throwaway datasource with a wrong password (`status=ERROR`, guard fires) alongside
  the real one (`status=OK`, guard passes) — then delete it. A guard you have not seen
  fail is not a guard; a guard whose verification pages the operator will not be
  verified twice.

- **`ignore_errors` + `register` whenever an assert reads `failed`.** Never
  `failed_when: false` in that position: it assigns `failed: False` and makes the
  assert vacuously true. Keep the reason at the task, not in a commit message
  (`ansible/roles/services/observability/tasks/main.yml:577-581`):

  ```yaml
    # ignore_errors, NOT failed_when: false — the assert below must be able to read
    # `failed`, and `failed_when: false` ASSIGNS failed=False, which would make the
    # assert vacuously true.
    ignore_errors: true
  ```

- **Size a verification window from the system's own period, and anchor it after the
  change.** "0 errors in the last 45 seconds" on rules that evaluate on an interval,
  measured across a restart boundary, is a sampling artifact rather than a result.
  Anchor to an absolute timestamp strictly after the restart and span enough time to
  contain multiple evaluation cycles of every rule under test.

- **A shared credential needs a written consumer list, and the list must record each
  consumer's UPDATE MECHANISM, not just its name.** `vault_vm_auth_password` is read by
  consumers in three different ways, and only the first is automatic:

  1. **Re-read from the templated `.env` on every deploy** — VictoriaMetrics' own
     `--httpAuth` flags, vector's remote-write sink, telegraf's output. A rotation
     reaches these for free.
  2. **Imported once into the application's own store** — Grafana's provisioned
     datasource. A rotation reaches it only if `version:` also advances. This is the
     subject of this document.
  3. **Hand-entered into a system this repo does not manage** — Home Assistant holds its
     own copy in a config entry created through its UI, so a rotation reaches it only
     when a human re-enters it, and nothing in any playbook will report that it did not.
     (Verified live 2026-08-22: Home Assistant is pushing to VictoriaMetrics and the
     series are arriving. Note the role README and PORT_REFERENCE still describe this
     integration as never having existed — accurate when written, overtaken since, and
     flagged for refresh. #133 remains open for the unrelated question of whether its
     exclusion filter works.)

  Nothing in the repo enumerated this asymmetry, so the rotation in #165 was reasonably
  believed to be a one-value change. A consumer list that names consumers without naming
  how each one updates is the list that produced this incident.

- **When a monitor shares a dependency with the thing it monitors, write that down
  where the monitor is defined.** `obs-alert-delivery-telemetry-absent` cannot report a
  VictoriaMetrics auth failure, because it queries VictoriaMetrics. That is not fixable
  by tuning the rule; it is a structural property, and the compensating control is the
  deploy-time assert added here — the only check in the loop that does not run on the
  observability stack's own health.

## Related

- CLAUDE.md → "A provisioned Grafana datasource is FROZEN unless its `version:`
  INCREASES" (added in PR #166) and "A guard you have not seen fail is not a guard".
- [docs/solutions/conventions/verification-instrument-must-distinguish-fixed-from-broken.md](../conventions/verification-instrument-must-distinguish-fixed-from-broken.md)
  — the general form: a check that cannot tell the broken state from the fixed one is
  not evidence.
- [docs/solutions/conventions/absence-alerts-need-a-continuously-exported-sentinel.md](../conventions/absence-alerts-need-a-continuously-exported-sentinel.md)
  — absence in a window is only evidence once you know how wide the window must be.
- [docs/solutions/conventions/prove-notification-delivery-not-just-config-validity.md](../conventions/prove-notification-delivery-not-just-config-validity.md)
  — same shape one layer out: config that parses is not a channel that delivers.
- [docs/solutions/integration-issues/grafana-alerting-provisioned-but-undeliverable.md](grafana-alerting-provisioned-but-undeliverable.md)
  — the other half of this stack's provisioning traps (rsync `delete: true` vs
  templated files, compose `${VAR:?}` quoting, the receiver-test endpoint).
- [docs/solutions/integration-issues/postgresql-mounted-configs-never-deployed-or-read.md](postgresql-mounted-configs-never-deployed-or-read.md)
  — the same "deployed is not wired" failure with a different mechanism; verify with
  the application's own introspection, never with `docker inspect`.
- PRs: #165 (the rotation that exposed it), #166 (the fix).

---
title: "`--diff` printed every vaulted secret in plaintext, and an empty auth var disabled VictoriaMetrics auth instead of failing"
date: 2026-08-16
category: security-issues
module: services/_deploy
problem_type: security_issue
component: authentication
symptoms:
  - "`ansible-playbook --check --diff` prints the full rendered `.env` for every service in scope — SMTP, DB and admin passwords, both the old and the new value on a rotation"
  - "The documented dry-run workflow (`task infra:hosts:check`, `--check --diff` before applying) is itself the leak: the safer the operator is being, the more secrets land in the transcript"
  - "VictoriaMetrics and VictoriaLogs start green, healthy and unauthenticated on 0.0.0.0:8428/9428 when their auth vault vars are empty — an empty `--httpAuth` value means auth DISABLED, not rejected"
  - "`env.j2` carried `| default('')` on all four auth vars, so a renamed or not-yet-set vault var became an empty string instead of a template failure"
  - "`scripts/validate-compose.sh` cross-checks mandatory `${VAR}` refs against `env.j2` but its regex required a closing brace, so the new `${VAR:?}` refs — the only truly mandatory ones — were invisible to it"
root_cause: missing_validation
resolution_type: code_fix
severity: critical
related_components:
  - services/observability
  - services/_deploy
  - services/searxng
  - nut
  - victoriametrics
  - victorialogs
  - ansible-vault
  - validate-compose
tags:
  - secrets
  - ansible-diff
  - no-log
  - fail-loudly
  - silent-green
  - http-auth
  - canary-testing
---

# `--diff` printed every vaulted secret in plaintext, and an empty auth var disabled VictoriaMetrics auth instead of failing

## Problem

Two independent secret-handling failures, both of which reported success the whole
time. First, every secret-bearing template task rendered its content into `--diff`
output, so the repo's own recommended dry-run workflow printed the fleet's secrets
to the terminal. Second, VictoriaMetrics and VictoriaLogs treat an **empty**
`--httpAuth.username/password` as *auth disabled* rather than as an error (upstream
documents the flag as "the authentication is disabled if empty"), and three separate
layers each converted a missing vault variable into an empty string — so a typo'd or
not-yet-set vault var deployed fully green with the metrics and logs APIs open to the
LAN.

## Symptoms

- A `--check --diff` run printed the complete rendered `.env` for every service in
  scope. On a rotation it printed **both** the retiring and the incoming secret,
  side by side, because a diff shows both sides.
- The same applied to NUT's `upsd.users` and `upsmon.conf` (UPS master/slave
  passwords) and to SearXNG's `settings.yml` (Brave API key).
- `curl http://<host>:8428/api/v1/query?query=up` with no credentials returned data
  whenever the auth vars were blank. `/health` returns 200 even when auth *is*
  configured (VictoriaMetrics exempts it by design), so a health probe cannot
  distinguish "auth on" from "auth off".
- Nothing failed. No warning, no non-zero exit, no unhealthy container. The only way
  to notice was to try an unauthenticated request by hand.
- `scripts/validate-compose.sh` reported `ok` for a compose file referencing a
  variable name that `env.j2` never emits, as long as the reference used `${VAR:?}`.

## What Didn't Work

- **`no_log: true` on the template tasks.** The obvious reflex, and wrong here for
  two reasons. The secret in this case lives in *rendered file content*, which is
  what `--diff` prints; the module's result dict and invocation args contain only
  paths and checksums, so `no_log` adds no coverage that `diff: false` does not.
  Worse, `no_log` also censors **failure** output, replacing it with "output has been
  hidden". A missing vault var fails the template task with `'vault_x' is undefined` —
  a message that contains no secret and is precisely the loud failure the second half
  of this issue depends on. Using `no_log` would have traded a closed leak for a new
  silent failure.
- **Binding the ports to the docker bridge** to solve the auth exposure. This breaks
  the actual consumers: Home Assistant reaches the influx listener over the LAN, and
  Grafana's browser-side users reach :3000. Port scoping is a separate, real problem
  (see Related Issues) but it is not a substitute for auth that works.
- **A naive non-empty assert.** The first version of the guard used
  `lookup('vars', item, default='') | length > 0`. It held for only two of the six
  ways a variable can be blank — see the matrix below.
- **Trusting `docker inspect` / a green deploy as proof.** Neither distinguishes
  "auth configured" from "auth disabled"; only an unauthenticated request does.

## Solution

### 1. `diff: false` on every secret-bearing template task

```yaml
# ansible/roles/services/_deploy/tasks/main.yml
- name: Template .env for {{ svc }}
  ansible.builtin.template:
    src: "{{ role_path | dirname }}/{{ svc }}/templates/env.j2"
    dest: "{{ svc_deploy_dir }}/.env"
    mode: "0600"
  # Every service's .env embeds vault secrets: --diff prints BOTH sides in
  # plaintext (#88). diff:false suppresses content but keeps changed:true and,
  # unlike no_log, keeps failure output ("'vault_x' is undefined") readable —
  # that loud failure is itself a control (#88 part 2).
  diff: false
```

One edit to the shared pipeline covers every service's `.env` fleet-wide, and no new
service can forget it. Anything that does **not** route its secret through `_deploy`
needs its own copy, and there are two such categories: a `services/*` role that
templates a secret somewhere other than the deploy dir (a sweep found exactly one —
`searxng`, which writes to the container's bind-mount path), and a non-service role
entirely (`nut`, three templates, which never touches `_deploy` at all). Current
sites:

- `ansible/roles/services/_deploy/tasks/main.yml` (line 37) — covers every service's `.env`
- `ansible/roles/nut/tasks/server.yml` (lines 69, 80)
- `ansible/roles/nut/tasks/client.yml` (line 29)
- `ansible/roles/services/searxng/tasks/main.yml` (line 26)
- `ansible/roles/services/postgresql/tasks/main.yml` (line 189) — predates this
  issue; it arrived with the vaulted postgres init password

To find the gaps in a fleet, list templates that reference vault variables and
subtract the ones that route through the shared pipeline:

```sh
grep -rl 'vault_' ansible/roles/*/templates/ ansible/roles/services/*/templates/ |
  grep -v '/env.j2$'
```

### 2. Three layers of fail-loudly, outermost first

**Layer 1 — assert, before anything touches state.** It names the offending
variable, and it runs under `--check`, so a dry run catches the problem too.

```yaml
# ansible/roles/services/observability/tasks/main.yml (first task in the role)
- name: Assert observability credentials are set and non-empty
  ansible.builtin.assert:
    that:
      - lookup('vars', item, default='') | default('', true) | string | trim | length > 0
    fail_msg: >-
      {{ item }} is empty or undefined — the observability stack must never deploy
      with blank credentials: VM/VL would run with HTTP auth silently disabled on
      0.0.0.0:8428/9428, and Grafana would start auth-weak on :3000. Set it with
      `task vault:edit -- inventory/host_vars/<host>/vault.yml`.
    quiet: true
  loop:
    - vault_vm_auth_username
    - vault_vm_auth_password
    - vault_vl_auth_username
    - vault_vl_auth_password
    - vault_grafana_user
    - vault_grafana_password
```

**Layer 2 — `env.j2` with no silent defaults.** Dropping `| default('')` means that
if the assert is ever bypassed, templating fails loudly instead of rendering an empty
credential (`ansible/roles/services/observability/templates/env.j2`, lines 8-11).

**Layer 3 — compose `${VAR:?}` as backstop.** This one guards a path Ansible cannot:
a human running `docker compose up` by hand in the deploy dir.

```yaml
# ansible/roles/services/observability/files/compose.yaml, lines 25-26
- "--httpAuth.username=${VM_AUTH_USERNAME:?required - VM must never run unauthenticated, see #88}"
- "--httpAuth.password=${VM_AUTH_PASSWORD:?required - VM must never run unauthenticated, see #88}"
```

Lines 78-79 carry the same two flags for VictoriaLogs, against `VL_AUTH_USERNAME`
and `VL_AUTH_PASSWORD`.

`:?` aborts the entire project parse, so it fires before any service starts.

### 3. The assert condition, filter by filter

Every filter in that chain fixes a case the previous version got wrong. Measured by
running both conditions over the same six inputs:

| Input | `\| length > 0` (naive) | `\| default('', true) \| string \| trim \| length > 0` |
| --- | --- | --- |
| undefined | FAIL, curated message | FAIL, curated message |
| empty string `""` | FAIL, curated message | FAIL, curated message |
| null (`vault_x:`) | **crash** — `object of type 'NoneType' has no len()` | FAIL, curated message |
| whitespace `"   "` | **PASS** — false negative | FAIL, curated message |
| normal `"obs"` | PASS | PASS |
| unquoted integer | **crash** — `_AnsibleTaggedInt has no len()` | PASS |

- `default('', true)` — the `true` makes it *falsy*-triggered, not just
  undefined-triggered, so a defined-but-null `vault_x:` (the natural way an operator
  blanks a value in YAML) becomes `''` instead of crashing `length`.
- `| string` — protects `trim` from a non-string value.
- `| trim` — rejects whitespace-only, which otherwise passes as "non-empty".

Falsy scalars (`0`, `false`) are also rejected as blank. That is a deliberate
trade: nonsense as a credential, and failing loud is the safe direction.

`quiet: true` trims success output. Values never print because the assertion is
reported as a literal expression and `fail_msg` names the *variable*, not its value.

### 4. Close the validator gap the `:?` form opened

`scripts/validate-compose.sh` cross-checks every mandatory `${VAR}` against the
role's `env.j2` so a rename cannot silently produce a blank. Its extraction regex
required a closing brace, so the `${VAR:?}` refs — the strongest possible claim that
a variable is required — were exempt from exactly that protection:

```sh
# scripts/validate-compose.sh:171-173
referenced="$(grep -vE '^[[:space:]]*#' "${file}" |
  grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*(\}|:\?)' |
  sed -E 's/^\$\{//; s/(\}|:\?)$//' | LC_ALL=C sort -u || true)"
```

`${VAR:-default}` stays excluded — it is self-sufficient by construction.

## Why This Works

**`diff: false` closes the exact channel and nothing more.** The leak was rendered
file content; `diff: false` suppresses content while leaving `changed: true` and the
full failure message intact. That preservation is not incidental — it is what lets
layer 2 work, because the loud `'vault_x' is undefined` failure is itself a control.
The right rule is about *where the secret lives*:

> Secret in **file content** → `diff: false`. Secret in **module args** → `no_log: true`.

The repo has precedent for both: `proxmox_guests` uses `no_log` because the API token
is passed as a module argument, where it would appear in `-vvv` invocation output;
the template tasks use `diff: false` because a template's args are just paths and
checksums. Reaching for `no_log` on a template is over-broad in a way that costs you
error visibility.

**The auth guard works because it is layered at different scopes.** The assert covers
the Ansible path and fires earliest, under `--check`, with the best message. `env.j2`
covers an assert that gets bypassed or a new host added without one. Compose `:?`
covers a human bypassing Ansible entirely. Each layer catches something the others
structurally cannot.

**Why an empty credential is worse than a wrong one.** A wrong password produces 401s
that someone notices immediately. An empty one produces a working, fast, green system
with the door open — the failure mode announces itself as success. This is the same
shape as the Vaultwarden truncated-`ADMIN_TOKEN` and unattended-upgrades cases below:
the software's own definition of "empty means off" turns a config mistake into a
silent security regression.

## Prevention

- **Give every new secret-bearing template or copy task `diff: false`.** Grep for
  vault-referencing templates outside `env.j2` (command above) when adding a service.
- **Never let a credential default to empty.** No `| default('')` on a secret in a
  Jinja template, and no `${SECRET:-}` on a flag whose empty value means "disabled".
  If a service can be legitimately run without a credential, that is an explicit
  opt-in var, not a silent fallback.
- **Assert credentials by name, before anything touches state**, and write the
  condition to survive null / non-string / whitespace values. A guard that crashes
  with a Jinja trace instead of its own message is only half a guard.
- **Prove a leak is closed with a canary dry-run — and prove the test can fail.**
  This is the technique that made the whole change verifiable without ever printing a
  secret:

  ```sh
  # 1. Inject a canary in place of the real secret and capture to a file.
  ansible-playbook playbooks/deploy-services.yml --check --diff --limit <host> \
    --tags <svc> -e vault_some_password=CANARY_88 > "$SCRATCH/canary.log" 2>&1

  # 2. The canary is fake, so it is safe to print. Expect 0.
  grep -c CANARY_88 "$SCRATCH/canary.log"

  # 3. Check the REAL secret via command substitution — the value is compared,
  #    never echoed. Expect CLEAN.
  S=$(ansible-vault view inventory/host_vars/<host>/vault.yml |
      awk -F': ' '/vault_some_password/{print $2}' | tr -d '"')
  grep -qF "$S" "$SCRATCH/canary.log" && echo LEAK || echo CLEAN

  # 4. Confirm the task still reports changed, with no diff hunk attached.
  awk '/^TASK \[/{t=$0} /^(@@|\+\+\+|--- )/{print t}' "$SCRATCH/canary.log"
  ```

  **Step 5 is the one people skip and it is the one that matters: run the identical
  test against the pre-change code.** A grep that finds nothing proves nothing on its
  own — the canary might simply never have reached the template. Checking out the old
  version of the single file, re-running, and seeing the canary appear (and the real
  secret leak) is what upgrades "found nothing" into "the suppression works". Every
  claim in this doc was verified that way. The counterfactual log contains a real
  secret, so grep it and delete it in the same script; never display it.

  Caveat learned the hard way: restoring with `git checkout HEAD -- <file>` reverts to
  the *committed* state, which silently discards the fix if it is not committed yet.
  Commit first, or re-apply and re-run after the counterfactual.

- **Verify auth with an unauthenticated request, not a health check.** The trio worth
  running after any auth change: no credentials → 401, wrong credentials → 401, vault
  credentials → 200. `/health` on VictoriaMetrics returns 200 either way and will
  happily tell you everything is fine.
- **When adding a new interpolation form to a compose file, check whether the
  validators still see it.** A guard keyed to a syntax pattern silently narrows the
  moment the syntax widens.
- **Prove a formatting-only compose change recreates nothing before it lands:**
  `docker compose config --hash='*'` against the old and new file under the same
  dummy `.env`; identical hashes for every service means no recreate. Comments and
  `:-` → `:?` are both stripped or resolved away, so hashes stay identical.

## Related Issues

- #88 — this issue.
- #122 — the influx listener on `:8089` is published on 0.0.0.0 with no auth;
  `--httpAuth.*` covers only the HTTP API, so it does not protect that port. Filed
  from this work, not fixed here.
- #117 — fleet-wide `env.j2` `$`-quoting sweep.
- [Vaultwarden: an unquoted `$` truncates the secret and the app accepts the wreckage](vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md)
  — the same "empty/broken credential accepted as valid" shape, one layer down in the
  dotenv parser.
- [Unattended-upgrades silently inert fleet-wide](unattended-upgrades-silently-inert-fleet-wide.md)
  — another stacked silent-green security failure.
- [Vector 0.57 silent log pipeline failure](../integration-issues/vector-057-silent-log-pipeline-failure.md)
  — the same stack, and the original case of a 401 that nothing reported.
- [Postgresql mounted configs never deployed or read](../integration-issues/postgresql-mounted-configs-never-deployed-or-read.md)
  — where the `diff: false` rule first appeared, for a single task. This issue is
  that rule applied fleet-wide and given its `no_log` boundary.
- [Collocating compose stacks into Ansible roles](../conventions/collocating-compose-stacks-into-ansible-roles.md)
  — narrates the earlier incident where a real SMTP password reached a transcript
  through `--diff`. Its suggested remedy ("plan for `no_log` or redaction") is
  superseded by the file-content-vs-module-args split above.
- [Ansible change-loop pitfalls](../conventions/ansible-change-loop-pitfalls.md)
  — already carries the "`no_log` censors failure output" half as a checklist item.
- [Scoped nftables on a live host](../conventions/scoped-nftables-on-live-host.md)
  — the pattern #122 should follow.
- [nftables `hook input` is inert for docker-published ports](../integration-issues/nftables-input-hook-inert-for-docker-published-ports.md)
  — why a naive firewall rule would not have saved this.

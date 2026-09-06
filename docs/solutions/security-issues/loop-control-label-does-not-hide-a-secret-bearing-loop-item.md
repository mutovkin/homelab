---
title: "`loop_control.label` does not hide a secret-bearing loop item — Ansible dumps the full `item` on every failure and on every result at -v"
date: 2026-09-05
category: security-issues
module: ansible
problem_type: security_issue
component: tooling
symptoms:
  - "A task looping over `{path, vars: {KEY: secret}}` entries with `loop_control.label: \"{{ item.path }}\"` printed the secret inside `\"item\": {...}` when its assert fired"
  - "The same value printed four times on a HAPPY-PATH `ansible-playbook -v` run — once per looped task — with `diff: false` set and no task failing"
  - "A copy task whose `content:` is `no_log` in its argument_spec still leaked the value: the leak was the loop `item`, not the module args"
root_cause: config_error
resolution_type: code_fix
severity: high
related_components:
  - ansible
  - secrets
tags:
  - ansible
  - loop-control
  - label
  - secrets
  - no-log
  - diff-false
  - verbosity
  - silent-failure
---

# `loop_control.label` does not hide a secret-bearing loop item — Ansible dumps the full `item` on every failure and on every result at -v

## Problem

#260 writes a sourced env file on the music workbench from a `workbench_env_files`
host_var whose entries are `{path, vars: {ACOUSTID_API_KEY: "{{ vault_… }}", …}}`.
The first revision looped four tasks (assert, mkdir, copy, verify) over those
entries with `loop_control.label: "{{ item.path }}"`, `diff: false` on the copy,
and an rc-only verify — and its comments and `docs/n5pro.md` said "the value never
logs". The review measured that claim false in three places: an assert failure, a
copy failure, and every ok result at `-v`.

`label:` only replaces the `(item=…)` prefix of the one-line status. The result
dict Ansible prints on failure — and for every ok/changed loop result at verbosity
≥ 1 — still contains `"item": <the whole entry>`. The copy module's `content`
parameter is `no_log` in its own argument_spec, so `invocation` was clean; the loop
item beside it was not.

## Symptoms

- `failed: [host] (item=/etc/mcl/env:KEY) => {"assertion": …, "item": [{"path": …}, {"key": "KEY", "value": "<secret>"}]}`
- `ok: [host] => (item=/etc/mcl/env) => {"changed": false, "item": {"path": …, "vars": {"KEY": "<secret>"}}, …}` on `-v`, once per task.
- The falsification runs in the PR looked clean only because their values were fake.

## What Didn't Work

- **`loop_control.label`** — cosmetic; see above.
- **`diff: false`** — suppresses the file diff only. Right tool for the content,
  irrelevant to the item.
- **`no_log: true`** — would hide the item, but also every failure message, which is
  the trade the repo already refused in #88 (an undefined vault key must fail BY
  NAME, readably).

## Solution

No loop item may carry a value. Loop over indices or key NAMES and reach the value
through task-level `vars:`, which are rendered per iteration and are not part of
the result (`ansible/playbooks/configure-guests.yml`, post_tasks of the workbench play):

```yaml
- name: Assert every env value is non-empty …
  ansible.builtin.assert:
    that:
      - workbench_env_value | string | trim | length > 0
    fail_msg: "{{ item.path }}: {{ item.key }} is empty …"   # names, never the value
    quiet: true
  vars:
    workbench_env_value: "{{ workbench_env_files[item.i].vars[item.key] }}"
  loop: "{{ workbench_env_keys | from_json }}"              # [{i, path, key}] — no values

- name: Write env files
  ansible.builtin.copy:
    dest: "{{ workbench_env_file.path }}"
    content: "…{{ workbench_env_file.vars … }}"
  vars:
    workbench_env_file: "{{ workbench_env_files[item] }}"
  loop: "{{ range(workbench_env_files | default([]) | length) | list }}"
  loop_control:
    label: "{{ workbench_env_file.path }}"
  diff: false
```

Measured after the rewrite: a full `-v` happy-path apply and a quote-falsification
failure both contain the value 0 times; the assert still fails by name.

Second fix from the same review: the verify task had `failed_when: rc != 0 and not
ansible_check_mode`, which turned an unsourceable or missing file into `ok` under
`--check` — on a converged host, the one run where drift should show. Gate on the
write task's own result instead: `when: not (ansible_check_mode and
workbench_env_write.results[item].changed)`. A converged dry-run now runs the
verify; only a file the dry-run could not write yet is skipped, visibly.

## Why This Works

The result dict is built from the task's registered fields plus the loop `item`;
task `vars:` are inputs to templating and never serialized into it. Indexing
into the source list from inside `vars:` keeps the loop's identity (path, key)
printable while the value stays out of every dump at every verbosity.

## Prevention

- Grep every loop that touches a vault value: `grep -rn "vault_" ansible | grep -i loop`
  is not enough — look for loops over structures that CONTAIN a templated vault
  reference. Any such `item` is a leak at `-v`.
- Test the leak with a fake, greppable value and a `-v` happy-path run, not only a
  failure run: `ansible-playbook -v … | grep -c FAKEVALUE` must be 0.
- `label:` is for readability. Never cite it as a control in a comment.

## Related Issues

- #88 — `diff: false` over `no_log`, and why failures must stay readable.
- [vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md](vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md) — the `$`-in-dotenv trap the single-quoting here also covers.
- [diff-leaks-vaulted-secrets-and-empty-auth-vars-disable-auth.md](diff-leaks-vaulted-secrets-and-empty-auth-vars-disable-auth.md) — the `--diff` half of the same family.

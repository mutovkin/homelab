---
title: "Vector interpolates env vars inside YAML comments, so prose naming a variable can fail the config load"
date: 2026-08-22
category: integration-issues
module: roles/vector_agent
problem_type: integration_issue
component: tooling
symptoms:
  - "x Missing environment variable in config. name = \"TZ\""
  - "Vector config load fails on a variable that appears nowhere except inside a comment"
  - "All hosts running a shared config template fail identically after a comment-only edit"
root_cause: config_error
resolution_type: config_change
severity: medium
related_components:
  - vector
  - vector_agent
  - systemd
  - ansible
tags:
  - vector
  - interpolation
  - env-var
  - yaml
  - comments
  - preflight-validate
  - guard-ordering
---

# Vector interpolates env vars inside YAML comments, so prose naming a variable can fail the config load

## Problem

A comment-only edit to `ansible/roles/vector_agent/templates/vector.yaml.j2` broke the Vector
config load on all three native log-shipper hosts. The comment mentioned an environment
variable in `${...}` form while explaining why the agents do *not* use it. No functional
line changed.

## Symptoms

```
x Missing environment variable in config. name = "TZ"
Failed to load ["/etc/vector/vector.yaml"]
```

- All three agents (`eq12`, `n5pro`, `n5pro_docker`) failed identically and simultaneously.
- `grep TZ /etc/vector/vector.yaml` finds the string only inside a `#` comment.
- The play failed at the config-validation task; **no shipper went down**.

## What Didn't Work

Nothing was tried and rejected here — the failure named its own cause in one line. What
is worth recording is the *reasoning* that produced the bug, because it is the reasonable
one: a comment is inert, so naming a variable inside one is documentation, not
configuration. That is true of most parsers. It is not true of this one.

## Solution

Name the variable in prose without the dollar-brace form.

```yaml
#   3. this file is templated, so ... `timezone` comes from a Jinja var rather than
#      from the container's ${TZ};                    # <- fails the load on the agents

#   3. this file is templated, so ... `timezone` comes from a Jinja var rather than
#      from the container's TZ environment variable.  # <- fine
```

The same sentence exists in the container's copy of the config, where `TZ` *is* defined,
so it interpolated harmlessly — and silently rewrote the comment text at load time. Both
copies were changed, because the wording is one copy-paste away from breaking the agent
again, and a comment whose text is substituted at load time is not worth keeping either
way.

Every surviving `${...}` in the agent template refers to a variable the systemd
`EnvironmentFile` really defines (`VECTOR_HOSTNAME`, `VL_AUTH_*`).

## Why This Works

Vector's environment-variable interpolation — enabled here by
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION`, which #73 turned on and which this
stack cannot run without — is a **textual pre-pass over the whole config file**. It runs
before the YAML parser, so it has no concept of a comment. There is no inert region: a
`${VAR}` anywhere in the bytes is a reference, and an undefined one is a fatal load error
rather than a warning.

The two configs differ only in whether the variable happened to exist:

| file | `TZ` in the environment? | result |
| --- | --- | --- |
| container `vector.yaml` (compose sets `TZ`) | yes | comment silently rewritten to the zone name |
| agent `vector.yaml` (systemd `EnvironmentFile`, no `TZ`) | no | **config load fails** |

This is the same family as two traps already recorded here, in different parsers, and
seeing the three together is the useful part:

- **Compose YAML** — an unquoted scalar *ends* at a ` #`, so
  `${VAR:?required - see #139}` reaches compose truncated and fails the whole-file parse
  ([grafana-alerting-provisioned-but-undeliverable](grafana-alerting-provisioned-but-undeliverable.md)).
  There a comment marker swallowed part of a real interpolation.
- **Compose dotenv** — an unquoted `.env` value containing `$` is silently truncated
  ([vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback](../security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md)).
- **Vector config** — prose inside a comment *becomes* a real interpolation. The mirror
  image of the first.

In all three the comment layer and the substitution layer are not separated the way the
author assumed, and in all three the blast radius is the entire file rather than the one
line.

## The half that made it free: the guard ran before the state change

This cost nothing, and that was not luck. `ansible/roles/vector_agent/tasks/main.yml` validates
the deployed config **before** it restarts anything:

```
:634  - name: Validate the deployed Vector configuration before restarting
:679  - name: Restart Vector for changed package or configuration
```

So the broken config reached disk, failed validation, and the play stopped. Each agent
kept running the config it had already loaded, and three healthy log shippers stayed
healthy. Reverse those two tasks and the same defect is a fleet-wide outage of the
pipeline that exists to tell you about outages.

Two things make this worth stating as a rule rather than an anecdote:

1. **This is the first time that guard ever caught a real defect.** It was added in #134
   and hardened twice before it ever fired — once because it ran as the wrong user
   through the wrong parser, once because `vector validate` takes its path positionally,
   so the flag form failed with `error: unexpected argument '--config' found` — loud, but
   invisible to `--check`, so it only surfaced on the first real apply. Both hardenings
   were made before the guard had ever caught anything. CLAUDE.md's
   rule that "a guard you have not seen fail is not a guard" cuts both ways: this one had
   not been seen to fire, and it was still worth the three commits it took to get right.
2. **A validate is a gate on the state change, not evidence of delivery.** It proves the
   config loads; it proves nothing about whether records arrive. That is why the role
   also runs an end-to-end ingest assert after the restart. Ranking the two correctly is
   the discipline in
   [prove-notification-delivery-not-just-config-validity](../conventions/prove-notification-delivery-not-just-config-validity.md).

## Prevention

- **Never write a `${...}` reference in prose** in a Vector config, even in a comment,
  even to say the variable is *not* used. Name it plainly: `the TZ environment variable`.
  This holds even for a reference that *would* resolve: it is silently rewritten to the
  value at load time, so the comment a future reader sees is not the comment that was
  written, and the same line copied into the other config breaks it.
- When a config format supports variable substitution, find out whether the substitution
  runs before or after comment stripping *before* writing prose that contains the sigil.
  Most parsers strip first; this one does not.
- **Keep validation ahead of the restart** in any role that deploys a config a daemon
  reads at start. The ordering is the whole value: a validate that runs after the restart
  reports a fact you can no longer act on.
- The two vector configs are a documented near-duplicate ("change one, change both"), and
  the drift-guard list at the top of the agent template is now five items. Extend that
  list in the same change that adds a sixth difference, or it silently stops guarding.

## Related Issues

- [vector-057-silent-log-pipeline-failure](vector-057-silent-log-pipeline-failure.md) —
  #73, which enabled the interpolation flag in the first place. Its "Why This Works"
  section describes interpolation as a *runtime* behaviour where Vector starts happily and
  only the receiving end rejects the request. That is true when the flag is **off**;
  with the flag on and a variable undefined, it is a **load-time** hard failure and Vector
  refuses to start. Both halves are now recorded.
- [vector-hostname-and-severity-labels-were-fabricated](vector-hostname-and-severity-labels-were-fabricated.md)
  — #143, which introduced the `VECTOR_HOSTNAME` label the surviving references read, and
  whose `rsyslogd -N1`-before-restart step is the same validate-then-change ordering,
  scoped to rsyslog. (Its `${VAR:?}` fail-loud idiom lives in `compose.yaml`, not in
  either vector config — inside a Vector config the references are bare `${VAR}`, and the
  fail-loud behaviour comes from Vector itself, which treats an undefined variable as a
  fatal load error with no default syntax needed.)
- [grafana-alerting-provisioned-but-undeliverable](grafana-alerting-provisioned-but-undeliverable.md)
  and
  [vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback](../security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md)
  — the sibling traps in the other two parsers.
- Found and fixed inside #154; no issue was filed for the trap itself.

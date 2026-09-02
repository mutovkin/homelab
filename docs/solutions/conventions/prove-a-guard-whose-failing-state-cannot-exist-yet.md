---
title: "When the failing state cannot exist yet: prove the guard on a real input of the same SHAPE, and write down which half stays unproven"
date: 2026-09-01
category: conventions
module: ansible
problem_type: convention
component: docker_host
severity: high
applies_when:
  - "writing a destructive guard for a condition that does not exist upstream yet (a release that has not shipped, a migration nobody has run)"
  - "an issue says the fix cannot be verified live and defers it indefinitely"
  - "choosing a substitute input to exercise a guard, and deciding what that substitute does NOT prove"
  - "using a regex backreference inside an ansible assert's `that:` conditional"
  - "a guard rejects an input that is in fact correct, while its own fail_msg renders as if everything matched"
  - "writing a safety check by re-implementing a package manager's dependency logic"
related_components:
  - docker_host
  - rocm
  - n5pro_docker
  - ansible
tags:
  - ansible
  - verification
  - falsifiability
  - guard-design
  - rocm
  - jinja
  - apt
---

# When the failing state cannot exist yet

## Context

"A guard you have not seen fail is not a guard" has an awkward corner: sometimes the failing
state **cannot be created**. #247 needed a reconcile that retires the previous ROCm release on a
`rocm_release` bump — but only ROCm 10.0 has ever been published, so there is no second release
to bump away from. #240 deferred the work for that reason, in as many words: "with only one
release in existence a destructive purge loop cannot be exercised against the state it targets."

Deferring forever is not the only option, and neither is shipping an unexercised destructive
loop.

This is a *different* unreachability from the one in
[a-falsification-run-must-actually-reach-the-guard.md](a-falsification-run-must-actually-reach-the-guard.md).
There, the guard was reachable in principle and the falsification run died upstream because the
variable it corrupted fed an earlier task; the fix was `-e` plumbing, or — when no `-e`
combination can reach the clause — evaluating the exact expression offline against measured
strings. Here nothing about the play is at fault: **the world has not produced the input yet.**

## Guidance

**Enumerate the guard's INPUTS, then look for a real input of the same shape.**

The reconcile does not read "a release bump". It reads four things: the set of installed
`amdrocm*` package names, `rocm_release`, `rocm_gfx_arch`, and the set of `/opt/rocm/core-*`
directories. A release bump and an *architecture* swap produce the same input shape for the
package half — a set of installed `amdrocm*` names outside the pinned release/arch, purged
through real apt and real dpkg. So `gfx1150 → gfx1200 → gfx1150` exercises the selection, the
guards and the purge on genuine state, today:

| Run | Observed |
| --- | --- |
| `-e rocm_gfx_arch=gfx1200` | stale set = exactly the 8 predicted `*-gfx1150` names; apt `8 to remove`, 430 MB freed, zero collateral |
| defaults (back) | stale set = exactly the 9 `*-gfx1200` names; `9 to remove`, 687 MB |
| defaults again | `changed=0` |

**Check the substitute really is the same shape, and name where it is not.** It was not identical:
an arch swap leaves `rocm_home` untouched, so it exercises the package half and **not** the tree
half. That gap is exactly where the branch's worst defect hid — a `--check` of a real release
bump went red at the ownership assert, telling the operator to fix pins that were correct,
because check mode only simulates the purge while the ownership probe reads real, still-owned
state. No arch run and no converged-host dry run could have produced it; it took a reviewer
reasoning about the shape difference.

**Grade each guard by the evidence that actually exists, in the code.** The ladder, best to worst:

1. driven red on the live host, through the play;
2. falsified offline against *measured* input from that host, with a passing positive control;
3. argued from the code.

Anything below rung 1 gets a named future occasion to be promoted. The role comment now reads:

```yaml
#   1. the pinned package must not itself be in the stale set …
#      Driven RED on CT 201 with `-e rocm_gfx_arch=gfx1200 -e
#      rocm_package=amdrocm10.0-gfx1150`: changed=0, purge never reached.
#   2. apt's own simulation of the purge must remove EXACTLY the stale set. …
#      Guard 2 has NOT been driven red through the play — no reachable role input
#      on this host produces collateral. Its INPUT was measured read-only …
#      and the guard was falsified OFFLINE against exactly that output, with a
#      passing positive control. The first real release bump is its first chance
#      to fire on real input.
```

An earlier draft claimed "two guards, both of which have been driven red on the live host". That
was false for guard 2, and a review caught it. **A false evidence claim in a comment is worse
than no claim** — it retires the very question the next reader should ask. (Same failure as #212,
where a comment asserted a deploy-time check that did not exist.)

**Write the unproven half down where it will be read on the day it matters** — a follow-up issue
with the exact commands, not a PR paragraph. #252 is that ledger for this change: the two
unobserved items, plus what to read on bump day.

## Why This Matters

The substitute run is not ceremony. It found two defects review had missed, and one of them can
only ever be found by *running* the guard.

### A `\N` backreference is lost inside an assert's `that:` conditional

The exact-set purge guard rejected a purge that was exactly right. Its own `fail_msg` — the
identical expression, evaluated through normal templating — rendered both difference lists as
empty:

```
Action failed: apt would not remove exactly the stale set.
Collateral (would go but was not asked for): []. Asked for but not removed: [].
"assertion": "... | map('regex_replace', '^(Remv|Purg) ([^ ]+).*$', '\\2') | list
              | symmetric_difference(rocm_stale_packages) | length == 0",
"evaluated_to": false
```

Empty differences and a false conditional cannot both be true of the same value. In the
conditional the group reference is lost, so every line replaces to the same literal, the list
collapses to one element, and the symmetric difference against eight names is non-empty.
Reproduced in isolation: the same expression under `debug:` prints the correct eight package
names; under `assert: that:` it evaluates false.

Template once, assert the fact:

```yaml
- name: Work out what apt says it would actually remove
  ansible.builtin.set_fact:
    rocm_purge_sim_removals: >-
      {{ rocm_purge_sim.stdout_lines | default([]) | select('match', '^(Remv|Purg) ')
         | map('regex_replace', '^(Remv|Purg) ([^ ]+).*$', '\2') | list }}

- name: Assert purging the stale ROCm packages takes nothing else with it
  ansible.builtin.assert:
    that:
      - rocm_purge_sim_removals | symmetric_difference(rocm_stale_packages) | length == 0
```

It failed *closed*, so it was never dangerous — it would simply have blocked every future release
bump, on a branch whose entire purpose is release bumps. **When an assert and its own `fail_msg`
disagree about the same data, suspect the conditional evaluator, not the data.** This belongs
with the Jinja items in
[ansible-change-loop-pitfalls.md](ansible-change-loop-pitfalls.md).

### Ask the tool to simulate; do not re-derive its dependency logic

The first revision proved safety by parsing `apt-cache depends --recurse --installed` and
asserting the stale set was disjoint from it. That was blind to anything depending on a stale
package from *outside* the pinned package's forward closure, and it failed OPEN: an empty reading
made the disjointness vacuously true and the purge proceeded unguarded — "a bound that fails open
when its own input is unvalidated is not a bound".

`apt-get -s -y purge <stale set>` answers the real question. Measured on CT 201: asking to purge
two arch packages simulates removing **six**, including the live metapackage. Compared as a set it
cannot fail open, because an empty simulation fails the comparison instead of passing it.

## When to Apply

- A guard's target condition depends on something that has not shipped yet.
- You are about to write "cannot be verified until X exists" in an issue. Ask what the guard's
  inputs actually are first; a different edit may produce the same input shape today.
- Any assert whose `that:` contains a regex backreference.
- Any safety check derived by re-implementing a package manager's or scheduler's dependency
  logic — prefer the tool's own `--simulate`/`-s`/`--dry-run` output, compared as a set.

## Examples

Falsifying a guard that no reachable input can trigger. The negative case feeds apt output
*measured on the host*; the positive case feeds the real output from the arch-swap run, so a
regression in either direction is caught:

```yaml
- name: Negative — the guard must refuse this purge
  block:
    - name: Negative — the role's assert, verbatim
      ansible.builtin.assert:
        that:
          - rocm_purge_sim_removals | symmetric_difference(rocm_stale_packages) | length == 0
        fail_msg: >-
          apt would not remove exactly the stale set. Collateral: …
  rescue:
    - name: Negative — record that the guard fired, and what it said
      ansible.builtin.set_fact:
        guard2_fired: true
        guard2_message: "{{ ansible_failed_result.msg }}"

- name: Negative — assert the guard fired and named the four collateral packages
  ansible.builtin.assert:
    that:
      - guard2_fired
      - "'amdrocm10.0-gfx1150' in guard2_message"
```

The `block`/`rescue` matters: it turns "the assert fired" into a green, repeatable observation
rather than a red run someone has to interpret, and asserting on `ansible_failed_result.msg`
proves the guard named the right packages rather than merely failing. Copy the expression from
the role rather than retyping it — a retyped copy tests the copy.

## Related

- [a-falsification-run-must-actually-reach-the-guard.md](a-falsification-run-must-actually-reach-the-guard.md)
  — unreachability caused by variable plumbing, and the offline-evaluation corollary this doc
  reuses. That doc's framing assumes the failing state is instantiable; this is the case where it
  is not.
- [verification-instrument-must-distinguish-fixed-from-broken.md](verification-instrument-must-distinguish-fixed-from-broken.md)
  — the contrast: there the remedy is a synthetic fixture, here it is a shape-equivalent REAL
  input on the live host. Prefer the real input where one exists; it exercises the tool, not your
  model of it.
- [experiment-must-discriminate-between-hypotheses.md](experiment-must-discriminate-between-hypotheses.md)
  — state the falsifier before measuring. #247's falsifier table was written before any run.
- [ansible-change-loop-pitfalls.md](ansible-change-loop-pitfalls.md) — the Jinja/assert pitfall
  family the backreference finding joins.

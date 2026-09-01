---
title: "A falsification run must actually REACH the guard — failing earlier in the play proves nothing about the assert"
date: 2026-08-31
category: conventions
module: ansible
problem_type: convention
component: docker_host
severity: high
applies_when:
  - "proving a new assert can fail, per 'a guard you have not seen fail is not a guard'"
  - "the variable you would corrupt to falsify the assert is also consumed by an upstream task"
  - "a package name, path, or URL is derived from the same var the assert reads"
  - "adding an apt repository and installing from it in the same role run"
  - "replacing a legacy installer stack with a one-way migration block"
related_components:
  - docker_host
  - rocm
  - n5pro_docker
---

# A falsification run must actually reach the guard

## Context

#240 replaced the legacy `amdgpu-install` ROCm 7.2.2 stack in CT 201 with ROCm 10.0
from AMD's stable apt repo, and replaced a `failed_when: false` + `debug` non-guard
with a real `assert` that ROCm delivers `gfx1150`.

CLAUDE.md already says *"a guard you have not seen fail is not a guard"*. The plan
duly specified a falsification run:

```bash
task infra:guests -- --limit n5pro_docker -e rocm_gfx_arch=gfx9999
```

It went red. **And it proved nothing about the assert.**

## The trap

`rocm_package` is derived from the same variable the assert reads:

```yaml
rocm_gfx_arch: "gfx1150"
rocm_package: "amdrocm{{ rocm_release }}-{{ rocm_gfx_arch }}"
```

So the override poisoned an *upstream* task first. The run died at

```
fatal: … "No package matching 'amdrocm10.0-gfx9999' is available"
```

— in the apt install, several tasks before the assert. The recap was red, the
override "worked", and the assert was never executed. A red recap from a
falsification run is not evidence that the guard fired; only the guard's own
`fail_msg` in the output is.

This is the same shape as the `failed_when: false` trap CLAUDE.md documents, one
level up: there, the assert ran and was vacuous; here, the assert was fine and
never ran. Both produce a confident "verified" with zero information.

## The rule

**Corrupt only the input the guard reads, and pin every upstream consumer back to a
working value, so the play reaches the assert.** Then read the guard's own message,
not the recap.

Both halves of #240's two-condition assert were exercised separately:

```bash
# A — the rocminfo half. Package pinned real so apt succeeds and the play continues.
task infra:guests -- --limit n5pro_docker \
  -e rocm_gfx_arch=gfx9999 -e rocm_package=amdrocm10.0-gfx1150
# [ERROR]: … rocminfo did not report gfx9999 … (dpkg says '10.0.0-4')

# B — the delivered-version half. Package AND home pinned real (rocm_home also
#     derives from rocm_release, and the assert runs rocm_home/bin/rocminfo).
task infra:guests -- --limit n5pro_docker \
  -e rocm_release=9.9 -e rocm_package=amdrocm10.0-gfx1150 -e rocm_home=/opt/rocm/core-10.0
# [ERROR]: … amdrocm10.0-gfx1150 is not at 9.9.x (dpkg says '10.0.0-4')
```

Both runs reported `changed=0`: a correct falsification run mutates nothing, which is
itself worth checking — if the falsification run changes host state, it is testing
something other than the guard.

Practical checklist before believing a falsification run:

1. Write down which task you expect to fail, **by name**, before running it.
2. Grep the role for every other consumer of the variable you are about to override.
   Pin each of those back with its own `-e`.
3. After the run, confirm the failing task is the guard and the output contains the
   guard's `fail_msg` — not just that the recap says `failed=1`.
4. For a multi-condition `assert`, do this once **per condition**. One red run proves
   one clause.

## The rule bites hardest on the guard you add *in response to a review*

Review round 1 of #240 flagged an unscoped recursive delete. The fix added what
looked like a belt-and-braces clause:

```yaml
    # never let it name the live install root no matter how the glob is edited
    - item.path != rocm_home
```

Round 2 showed it **could not fire**. `find` ran with `recurse: false` over `/opt`,
so `item.path` is always a direct child, while `rocm_home` is `/opt/rocm/core-10.0`
— two levels down. And the scenario the comment promised to cover was exactly the
one it missed: widen the glob to `rocm*` and `find` returns `/opt/rocm`, the live
install root, which `!= /opt/rocm/core-10.0` passes — recursively deleting ROCm.
The correct clause is a prefix test that rejects the root **and any ancestor**:

```yaml
    - not (rocm_home ~ '/').startswith(item.path ~ '/')
```

Two things generalise. **A fix written under review pressure gets the least
scrutiny of anything in the change** — it arrives late, it looks defensive, and
"belt-and-braces" reads as obviously safe. It is a guard like any other and owes
the same falsification. And **a guard whose only justification is a hypothetical
must be tested against that hypothetical**: this one was justified by "no matter
how the glob is edited", so the test had to actually edit the glob. The test that
landed asserts both that the new clause matches intent on every path and that the
old clause was *wrong* on `/opt/rocm` — a test that would have failed before the
fix, which is the only kind worth writing.

This happened in the same change that added this document.

## Corollary: when a clause cannot be reached live, falsify it offline against measured strings

Sometimes no `-e` combination can reach a clause, because the variable feeds an
upstream task that must succeed for the play to get there. #240's
`startswith('ii ')` clause is one: `rocm_package` feeds both the apt install and
the dpkg query, so the only way to make the query report a non-`ii` state is to
point both at a package that is not installed — which the install task then
installs.

The answer is not to skip the test. Measure the real strings read-only, then
evaluate **the exact expression** against them in a throwaway playbook, negatives
included:

```
ii |10.0.0-4              state ok  version ok    <- the real package
rc |2.15.0-1.1ubuntu2     state NO  version NO    <- measured on a real `rc` package
iU |10.0.0-4              state NO  version ok    <- unpacked, never configured
""  (empty stdout)        state NO  version NO    <- must not raise
```

That last row earned its place: `''.split('|')[1]` raises `IndexError`, which
would have replaced the assert's curated `fail_msg` with a Jinja traceback. Model
the empty and missing cases explicitly — and note that Jinja's `default()`
substitutes for **Undefined only**, never for `None`.

## `dpkg-query -W` exits 0 for a package in ANY state

`dpkg-query -W -f='${Version}' <pkg>` returns rc 0 and prints a populated version
for `iU` (unpacked, never configured), `iF` (half-configured) and `rc` (removed,
config files remain) — not just `ii`. Measured on CT 201 against a package the
migration itself left behind:

```bash
$ dpkg -l fontconfig | tail -1
rc  fontconfig  2.15.0-1.1ubuntu2 ...
$ dpkg-query -W -f='${Version}' fontconfig     # -> 2.15.0-1.1ubuntu2, rc=0
```

So "the package is installed" cannot be asserted from a version string. Ask for
the state too — `-f='${db:Status-Abbrev}|${Version}'` — and require `ii `.
Otherwise a package that unpacked and failed its `postinst` satisfies the assert
with its files sitting unconfigured on disk, and if the binary landed, a
`rocminfo`-style check passes alongside it.

## A derived file is state that can go stale; prefer no derived file

The draft downloaded AMD's armored key to `amdrocm.asc` and `gpg --dearmor`'d it
to `amdrocm.gpg` with a `creates:` gate. On key rotation `get_url` refreshes the
`.asc`, the dearmor is skipped because the `.gpg` exists, and apt keeps verifying
against the stale key — an unhealable `NO_PUBKEY` that only a manual `rm` fixes,
which the repo's "no ad-hoc SSH" rule forbids. This is the ISO rule
(`docs/solutions/conventions/ansible-change-loop-pitfalls.md`) wearing a
different hat: an existence gate over a *derived* artefact trusts a stale copy
forever.

The fix was not a better gate but **removing the derived file**: apt's
`Signed-By:` accepts an armored key directly (the same role already relies on
this for Docker's `docker.asc`), so the file `get_url` rewrites *is* the file apt
reads and rotation reconciles itself. When a reconcile has a stale-derivative
problem, ask first whether the derivative needs to exist. A registered-but-never-
read result variable is the tell that the wiring was written and then not used.

## In a one-shot block, the step that clears the gate's signal must be LAST

The migration block probed `dpkg-query amdgpu-install` and, inside the gate, ran
`uninstall → purge amdgpu-install → autoremove`. The **purge is the probe's
signal.** A dpkg lock or a full disk on the autoremove — both plausible right
after purging a multi-GB stack — ends the play red; the re-run then finds
`amdgpu-install` gone, skips the whole block, and orphans the old packages
forever, silently. Reordering to `uninstall → autoremove → purge` closes the
window outright: any death before the purge leaves the gate armed. Cheaper and
less state than the write-ahead intent file #127 needed, and it generalises —
**in any probe-gated one-shot block, order the steps so the one that consumes the
probe's signal runs last.**

## Two supporting traps from the same change

**`--check` cannot install from a repo it did not write.** Adding a deb822 `.sources`
file and installing from it in the same role means the first dry-run on a host that
lacks the repo dies with `No package matching '<pkg>' is available`, because check
mode never wrote the file. That is a real defect (the documented dry-run-before-apply
workflow has to work), and the fix is a carve-out narrow enough that it cannot hide
drift:

```yaml
  when:
    - gpu_sharing.enabled | default(false)
    - not (ansible_check_mode and rocm_repo_file.changed)
```

On a host where the repo file is already correct, `rocm_repo_file.changed` is false
and the task runs under `--check` as normal.

**A vendor uninstaller can delete itself, so gate it on its own existence.**
`amdgpu-uninstall` removes `/usr/bin/amdgpu-uninstall` as its last act (and forwards
unrecognised arguments — including `-y` — verbatim to the `apt-get purge` it runs,
which is the only reason it is non-interactive). A run that dies between the
uninstall and the follow-up purge would otherwise re-enter the block and die on a
missing binary. It also means only the *destructive* steps belong inside a one-shot
probe gate: the purge clears the probe's own signal, so every "make this absent"
reconcile (repo lists, `update-alternatives` entry, the `/opt/rocm` symlink, leftover
`/opt/rocm-<ver>` trees) lives **outside** the gate, keyed on its own durable state.
That is the #127 lesson applied to a migration block.

**Pin the assert's path from the artefact, not from the docs.** The doc said ROCm
"installs to standard system paths like `/opt/rocm`". Unpacking
`amdrocm-base10.0_10.0.0-4_amd64.deb` locally before the first apply showed
`./opt/rocm/core-10.0/bin/rocminfo` and no `/opt/rocm/bin` compatibility link — so the
assert path and `/etc/profile.d/rocm.sh` were right on the first live run instead of
needing a fix-and-rerun cycle. It also surfaced that `/opt/rocm` had to stop being an
`update-alternatives` symlink before dpkg could unpack a directory there.

## See also

- CLAUDE.md — "A guard you have not seen fail is not a guard"; "Arm a guard from
  durable state, not a one-shot `changed`".
- `docs/solutions/conventions/experiment-must-discriminate-between-hypotheses.md` —
  the same discipline applied to measurement rather than to guards.

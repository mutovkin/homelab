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

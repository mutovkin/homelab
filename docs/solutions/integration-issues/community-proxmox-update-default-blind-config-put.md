---
title: "community.proxmox flipped `update` to true: `state: present` now blind-PUTs the whole LXC config on every run"
date: 2026-08-16
category: integration-issues
module: proxmox_guests
problem_type: integration_issue
component: tooling
symptoms:
  - "Every infra:hosts run reports changed=True for an already-existing LXC whose config is byte-identical before and after"
  - "`pct config <vmid>` shows no diff, yet the play never converges — an immediate second run is changed again"
  - "A full-kwargs config PUT lands on netif/rootfs/mp of a live container on every run, re-asserting synthesized disk strings over the real allocated subvols"
  - "Tags never converge: the module compares a Python list-repr against the API's plain string, and description comes back URL-encoded with a trailing %0A"
  - "`--check --diff` reveals none of it: neither community.proxmox.proxmox nor community.proxmox.proxmox_kvm supports check mode"
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - proxmox
  - proxmox_kvm
  - community.proxmox
  - lxc
  - ansible
tags:
  - ansible
  - community-proxmox
  - proxmox
  - lxc
  - idempotency
  - silent-failure
  - check-mode
  - breaking-change
---

# community.proxmox flipped `update` to true: `state: present` now blind-PUTs the whole LXC config on every run

> **Source paths in this document.** `plugins/modules/…` line references are relative to the
> **installed collection**, not this repo:
> `~/.ansible/collections/ansible_collections/community/proxmox/` at version **1.6.0**.
> `ansible/module_utils/…` is inside **ansible-core 2.20.5**, not this repo's `ansible/`
> directory. Repo paths are always written in full from the repo root
> (`ansible/roles/proxmox_guests/…`). Line numbers were verified against those installed
> versions on 2026-08-16 and will drift with upgrades — re-grep the symbol, not the line.

## Problem

`community.proxmox` 1.0.0 flipped the `proxmox` module's `update` default from `false` to
`true`. Nothing in this repo changed — the collection did. From that upgrade on, the
`Provision LXC containers` task in `ansible/roles/proxmox_guests/tasks/main.yml` stopped being
a create-only task: for every container that already existed, `state: present` took the
*update* branch, computed a diff that can never come out empty, and PUT the **entire** desired
config back to `/nodes/<node>/lxc/<vmid>/config` — netif, rootfs, mountpoints and all — then
reported `changed=True` unconditionally.

The visible cost was a permanently-red-in-green-clothing task: `changed` on every apply,
converging nothing. The real cost was that a blind full-config PUT was firing at live guests on
every `task infra:hosts` run, including one carrying `mp0` in *allocation* syntax at a
container whose data mount is a named subvol. Fixed under #86.

## Symptoms

- `Provision LXC containers` reports `changed` for **every already-existing CT on every live
  apply**, and stays `changed` across two consecutive applies of the same unmodified branch.
  Idempotence never arrives.
- `pct config <vmid>` is byte-identical before and after the run that reported `changed`. The
  task is not lying about having written — it is lying about having *changed* anything, because
  `exit_json(changed=True, ...)` at `plugins/modules/proxmox.py:894` is unconditional and runs
  regardless of what the API did with the PUT.
- `--check --diff` shows nothing at all. Neither `community.proxmox.proxmox` nor
  `community.proxmox.proxmox_kvm` declares `supports_check_mode`, so the task is **skipped** in
  a dry run. The documented dry-run-first workflow is structurally blind to this.
- On n5pro, `/etc/pve/lxc/201.conf` carries a `[pve:pending]` section staging a key-reordered
  `mp0` — `local-zfs:subvol-201-disk-1,size=200G,mp=/data` against an active
  `local-zfs:subvol-201-disk-1,mp=/data,size=200G`. Semantically identical (same volume, same
  mountpoint, same size — only key order differs), so nothing was lost; but a pending mountpoint
  write that could not be applied to the running container is the fingerprint of exactly this
  PUT landing, run after run.

## What Didn't Work

- **Blaming `pubkey`.** This is the near-miss worth recording, because the evidence for it is
  sitting inside the very function that computes the diff. `proxmox.py:1056-1059` translates
  `pubkey` into `ssh-public-keys`:

  ```python
  # plugins/modules/proxmox.py:1056  (inside update_lxc_instance)
  if "pubkey" in kwargs:
      pubkey = kwargs.pop("pubkey")
      if self.version() >= LooseVersion("4.2"):
          kwargs["ssh-public-keys"] = pubkey
  ```

  `ssh-public-keys` is a create-only key that never appears in a stored container config, so
  `arg not in current_config` would be permanently true and the diff permanently non-empty.
  Clean theory, correct mechanism, wrong culprit: **this branch is dead on the update path.**
  `lxc_present()` passes an explicit, hand-written kwarg list into `update_lxc_instance()`
  (`proxmox.py:869-892`) and `pubkey` is simply not in it. The only caller that supplies
  `pubkey` is `create_lxc_instance()`, whose own copy of the same translation lives at
  `proxmox.py:1201-1206`. Two identical blocks, one live and one unreachable, ~145 lines apart.
  Read the caller's argument list before trusting a callee's branch.
- **Assuming the diff gate would save us.** `if not diff: exit_json(changed=False, ...)` at
  `proxmox.py:1086-1087` reads like idempotence. It is not: the diff is only a *gate*, and what
  gets written past it is `**kwargs` — the whole desired config — not `diff`
  (`proxmox.py:1090`). One un-matchable field is enough to push every other field onto the wire.
- **Trusting `--check --diff` as the safety net.** The repo's Critical Rule 5 is dry-run before
  live, and it cannot see this class at all. Worse, check mode also never validates the module's
  *arguments*: a mutually-exclusive or required-if violation introduced here would first surface
  on the live apply.
- **Waiting for a failure.** Nothing ever failed. Every run since the role was written
  (`ansible/roles/proxmox_guests/tasks/main.yml`, first committed 2026-04-28) was a green play.
  There was no failing run to investigate — which is the whole difficulty of this class.

## Solution

Pin the LXC module **create-only** and move every update into an explicit, guarded reconcile
task in the same file.

Before (`ansible/roles/proxmox_guests/tasks/main.yml`, as on `master`):

```yaml
- name: Provision LXC containers
  become: false
  community.proxmox.proxmox:
    …
    tags: "{{ item.tags | default([]) }}"
    description: "{{ item.description | default('') }}"
    startup: "{{ item.startup | default(omit) }}"
    state: present
  delegate_to: localhost
  loop: "{{ proxmox_lxcs }}"
```

After — the same task, with the flip pinned and the reason written down at the call site:

```yaml
    state: present
    # CREATION ONLY — do not remove.
    #
    # community.proxmox 1.0.0 flipped this module's `update` default from false
    # to true (1.6.0 argspec: update=dict(type="bool", default=True)). With it
    # true, `state: present` on an EXISTING container runs update_lxc_instance()
    # every single run, and that path:
    #   (a) never converges …
    #   (b) on ANY non-empty diff PUTs the FULL kwargs set, not the diff …
    update: false
```

The VM task gets the same pin **even though `proxmox_kvm`'s default is still `false`**
(`plugins/modules/proxmox_kvm.py:1378`), precisely so the next minor bump cannot repeat the
trick:

```yaml
- name: Provision virtual machines
  community.proxmox.proxmox_kvm:
    …
    state: present
    # CREATION ONLY. proxmox_kvm's `update` still defaults to false, but pin it
    # explicitly so a future collection bump cannot silently flip this task into
    # the same blind config PUT that bit the LXC task below.
    update: false
```

Both module calls are now create-only, and the role converges existing guests through named
tasks it owns end to end: `Apply cores/memory to LXC containers` (`pct set`, hot — cgroup
limits retune a running CT with no restart); `Apply cores/memory to stopped VMs` (`qm set`,
behind the fail-closed `Refuse to change cores/memory on a VM that is not confirmed stopped`
gate); `Reconcile startup/onboot on existing …` for both guest types; the MAC-preserving
`Update VM network interfaces on existing VMs`; `Set features for privileged LXC containers`;
and `Converge drifted cluster PCI mappings`, content-compared against `host_vars`. Everything
not on that list is create-time only, and the file's header comment says so.

**Argument safety had to be established without a live run**, since check mode skips the task
and therefore never validates args. Three facts settle it from source:

- `update` is mutually exclusive only with `clone` and `force` for LXC
  (`proxmox.py:765-766`) and with `delete`, `revert`, `clone` for KVM
  (`proxmox_kvm.py:1393-1395`). Neither task passes any of them.
- The defaulted `force=False` (`proxmox.py:720`) cannot collide with the explicit `update`,
  because ansible-core runs `check_mutually_exclusive()` **before** defaults are applied —
  `ansible/module_utils/common/arg_spec.py:229` versus the real
  `_set_defaults(...)` at `:249` (the earlier call at `:233` passes `set_default=False` and only
  harvests `no_log` values). Mutual exclusion sees user-supplied parameters only.
- `required_if` for `state=present` is the any-of form
  `("state", "present", ("clone", "ostemplate", "update"), True)` (`proxmox.py:755`), already
  satisfied by `ostemplate` independently of `update`.

Verification an operator actually runs:

```bash
# 1. What is the installed default, right now?
ansible-galaxy collection list community.proxmox
ansible-doc community.proxmox.proxmox | grep -B2 -A6 'default changed from'
#   -> "The default changed from `false' to `true' in community.proxmox 1.0.0."

# 2. Freeze the live config, apply, compare. This is the assertion that matters.
ssh root@192.168.25.5 'pct config 101' | shasum -a 256
task infra:hosts -- --limit eq12
ssh root@192.168.25.5 'pct config 101' | shasum -a 256   # must be identical

# 3. Apply twice. "Provision LXC containers" must be ok, not changed, on run two.
task infra:hosts -- --limit eq12

# 4. No mountpoint write is being staged behind the running container.
ssh root@192.168.30.5 'cat /etc/pve/lxc/201.conf'        # no [pve:pending] mp0
```

## Why This Works

The update path could not converge, for three independent reasons, and any one of them is
enough. All three live in the diff loop at `proxmox.py:1069-1084`:

```python
for arg, value in kwargs.items():
    if arg not in current_config:
        diff[arg] = value
    elif isinstance(value, str):
        # compare all string values as lists as some of them may be lists separated by commas
        current_values = current_config[arg].split(",")
        requested_values = value.split(",")
        for new_value in requested_values:
            if new_value not in current_values:
                diff[arg] = value
                break
    elif str(value) != str(current_config[arg]):
        diff[arg] = value
```

1. **`tags` is still a Python list at compare time.** `lxc_present()` passes
   `tags=self.params.get("tags")` straight through (`proxmox.py:891`) and nothing stringifies
   it before the loop, so it misses the `isinstance(value, str)` branch and lands on the
   final `str(value) != str(current_config[arg])`: `"['docker']"` versus `"docker"`. Never
   equal. This fires on **every container that has tags at all** — CT 101 carries
   `tags: [docker]` in `ansible/inventory/host_vars/eq12/vars.yml:73-74`.
2. **`description` comes back URL-encoded.** `pct config 101` returns:

   ```
   description: Primary Docker host %E2%80%94 runs all container services (observability, postgresql, vaultwarden, etc.)%0A
   ```

   The desired value is raw UTF-8 with no trailing newline. Both sides are strings, so this
   goes down the comma-split membership check — and the segment containing the em-dash
   (`Primary Docker host — runs all container services (observability`) is not found among the
   encoded current values. Never equal.
3. **`disk` is re-synthesized, not read back.** `kwargs.pop("disk")` feeds
   `process_disk_keys()` (`proxmox.py:1332`), which routes through `build_volume()`
   (`proxmox.py:1473`) to produce a `rootfs` string built from `storage` + `size`. The stored
   value is the *allocated* volume, `local-zfs:subvol-101-disk-0,size=24G`. A synthesized
   allocation request cannot equal a realized volume name. Never equal.

So `diff` is non-empty on every run, forever. And the moment it is non-empty, line 1090 writes
everything:

```python
getattr(proxmox_node, self.VZ_TYPE)(vmid).config.put(vmid=vmid, node=node, **kwargs)
```

`**kwargs`, not `**diff`. The diff is a gate on *whether* to write, never a description of
*what* to write. Then `proxmox.py:894` reports `changed=True` without consulting the API's
answer at all.

That is why this is a hazard and not a cosmetic idempotence wart. On n5pro, CT 201's `mp0` is
declared in `ansible/inventory/host_vars/n5pro/vars.yml:178` as
`local-zfs:200,mp=/data` — allocation syntax, "give me a new 200G volume" — while the live
container runs `local-zfs:subvol-201-disk-1,mp=/data,size=200G`. Every run PUT the allocation
form at a container whose data mount is a named subvol. The `[pve:pending]` block on
`/etc/pve/lxc/201.conf` is the corroborating artifact: a mountpoint write that reached pmxcfs
and could not be applied to a running container. It happens to be semantically identical to the
active value, so no data moved — but it demonstrates the write path was live, repeatedly, at
the one field where getting it wrong costs a data volume. (Reconciling that allocation-vs-named
declaration is #87, deliberately out of scope for #86.)

Pinning `update: false` sends `lxc_present()` down the `elif not force:` branch at
`proxmox.py:895-898` instead — `exit_json(changed=False, vmid=vmid, msg="VM … already
exists.")`. No config read, no diff, no PUT. `state: started` is unaffected: the dispatch at
`proxmox.py:816` calls `lxc_started()`, which never receives `update` at all.

## Prevention

- **`state: present` is not a promise of create-only.** Treat "does this module *update* an
  existing object, and does its default say yes?" as a question you answer from the argspec,
  not from the state name. Read `ansible-doc <fqcn> | grep -A6 '^  update:'` before adopting a
  provisioning module, and again after any collection bump.
- **Pin the create/update intent explicitly on every provisioning module call, even when the
  current default already matches what you want.** `update: false` on `proxmox_kvm` is a no-op
  today and is in the file anyway. A default you rely on but never write down is a default that
  can be changed by someone else's release notes.
- **Minor collection upgrades change behaviour.** `community.proxmox` 1.0.0 flipped a
  destructive-adjacent default and documented it in one line of module docs. Read the changelog
  for `default` flips whenever `ansible-galaxy collection install --upgrade` runs, and prefer
  pinning collection versions in `requirements.yml` so the bump is a reviewable diff rather
  than an ambient event.
- **`--check` cannot see a task the module skips.** Any module without `supports_check_mode`
  vanishes from a dry run — a green `--check --diff` over a provisioning role proves the role
  parses, nothing more. It also does not validate module arguments, so an argspec violation
  introduced under check mode first appears on a live apply; verify `mutually_exclusive` /
  `required_if` against the collection source instead. Where the dry run must still say
  something useful, do what the reconcile tasks in this role do: `check_mode: false` on the
  read-only probe, plus an `ansible.builtin.debug` drift report that runs in check mode.
- **`changed` that never becomes `ok` is a bug report, not noise.** Persistent `changed` on an
  unmodified branch means either the module is not idempotent or your desired state is
  unreachable. Both are worth an hour. The cheap discriminator: hash the live config
  (`pct config <id> | shasum -a 256`) either side of the apply — if the hash holds while the
  task claims `changed`, the module's `changed` is fabricated and you should go read its
  `exit_json`.
- **A diff that gates a write is not a diff that scopes it.** Before trusting any module's
  "only changes what drifted" behaviour, find the actual write call and check whether it is
  passed the diff or the full desired state. `config.put(..., **kwargs)` is the tell.
- **Declare live resources in the syntax they actually exist in.** `local-zfs:200` (allocate)
  and `local-zfs:subvol-201-disk-1,size=200G` (this volume) are different requests wearing the
  same field name. Pin `host_vars` to what `pct config` reports for anything already running,
  and make renumbering or reallocation a separate, deliberate change — the same rule the
  Portainer `.env` subnet gotcha produced.

## Related Issues

- #86 — this fix: pin `update: false`, make both provisioning modules create-only, and move
  guest reconcile into explicit guarded tasks.
- #100 — **closed as completed, but neither symptom was actually fixed, and both root causes
  were wrong.** It reported the same two always-`changed` tasks and attributed them to missing
  `changed_when` guards on the "localhost Proxmox API loop" and to the `pct set -features` task
  "re-applying flags that are already set". #86 found different causes for both: the module task
  is `changed` because `update: true` really is issuing a config PUT and
  `exit_json(changed=True, …)` is unconditional, and the features task never applied anything at
  all (see [[lxc-features-nfs-invalid-key-silent-green]]). Its remedy for the first offered two
  options — *"either set `changed_when: false` (if it is a read/lookup) or derive `changed_when`
  from the API response"* — and both fail here, for the same reason the diagnosis did. The task
  is not a read/lookup, so the first option would have silenced the alarm while the blind PUT
  continued, which is strictly worse than the noise; and the API response cannot be derived from
  either, because `changed=True` is returned unconditionally without consulting it. Verified
  2026-08-16: no commit references #100, and both defects were still present on `master` at
  `d56eff5`.
- #119 — direct follow-on: the pre-existing NIC and startup/onboot reconcile shells read
  `qm`/`pct config` unguarded and are invisible in check mode.
- #87 — `mp0` / `ide2` declared in `host_vars` in a form that does not match the live guest.
  Rebuild fidelity of create-time-only fields; deliberately out of scope for #86.
- #36 — the earlier half of the same create-only gap: `startup`/`onboot` added to vars never
  reached an already-existing guest, so TrueNAS silently lost `order=1` and booted after its
  NFS consumer.
- [[lxc-features-nfs-invalid-key-silent-green]] — the sibling defect found in the same role
  during #86: a shell task that echoed `changed` while `pct set` rejected an invalid feature key
  on every run.
- [[proxmox-boot-order-inversion-breaks-nfs-volume-mount]] — the direct predecessor, and now
  **partially contradicted**. It concluded that `community.proxmox` with `state: present` does
  not update an existing guest. That was true of the collection version it was written against;
  on >=1.0.0 the LXC module updates aggressively. Its remedy (explicit reconcile tasks) is still
  correct — only its stated mechanism is stale.
- [[unattended-upgrades-silently-inert-fleet-wide]] — the same shape one layer up: Ansible
  variable precedence quietly making configuration inert while the play stays green.
- [[postgresql-mounted-configs-never-deployed-or-read]] — a guard that never fires is a guard
  that was never tested, and check mode is where guards go to hide.
- [[compose-up-recreates-watchtower-created-containers]] — companion rule for reading `changed`
  honestly: a reported change is evidence about the tool, not proof about the system.
- [[ansible-change-loop-pitfalls]] — the repo's idempotency-gate checklist. Its check-mode
  section assumes a task can be made check-mode safe; a module without `supports_check_mode` is
  the case it does not yet cover.

---
title: "Collocating Compose stacks into Ansible roles without recreating containers"
date: 2026-08-15
category: conventions
module: ansible
problem_type: convention
component: tooling
severity: high
applies_when:
  - "moving or renaming a deployed Compose stack, service, or its deploy directory"
  - "collocating repo payload into an Ansible role's files/ directory"
  - "deleting a tracked file that a gitleaks allowlist rule covers"
  - "a service is about to receive its first-ever templated .env"
  - "a task consumes a file that an earlier task in the same run ships"
related_components:
  - docker-compose
  - ansible-lint
  - gitleaks
  - services/_deploy
  - portainer
  - lms
tags:
  - ansible
  - docker-compose
  - ansible-lint
  - gitleaks
  - check-mode
  - rsync
  - compose-project-name
  - collocation
---

# Moving a live Compose stack without recreating it

## Context

`containers/<svc>/` held every Docker Compose stack; `ansible/roles/services/<svc>/`
held the Ansible role that deployed it. One service, two directories, and nothing
enforcing that they agreed — a mirror-pair drift class. SearXNG showed the failure
mode plainly: `containers/searxng/config/settings.yml` (deleted by this change) sat
next to the compose file looking authoritative while the file Ansible actually
rendered was `ansible/roles/services/searxng/templates/settings.yml.j2`. Editing the
wrong one changed nothing, convincingly.

That class had already cost real downtime. The Vector 0.57 incident
(`docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md`) ran a
dead log pipeline for roughly a month because the repo's `vector.yaml` was decorative
and the live file was whatever had last been hand-copied. PostgreSQL still has the
identical defect on all four of its bind-mounted configs (#78).

Issue #85 collapsed the two directories into one: each stack moved to
`ansible/roles/services/<svc>/files/compose.yaml`, and the eleven copy-pasted deploy
blocks (four to five near-identical tasks each) became a single parameterized role,
`services/_deploy`. The
constraint that made it interesting was not the move, it was the bar the move had to
clear: **the on-host deploy payload byte-identical, and zero container recreations**.
A refactor that restarts the vault, renumbers a network, or bounces Grafana is not a
refactor.

Under that constraint the dry run stopped being a formality and became the
instrument. It surfaced two latent hazards that had nothing to do with moving files
— they were pre-existing conditions the move would have *triggered*. This document
is about both the hazards and the method that caught them.

Status as written: the work landed on branch `fix/85-collocate-containers`, unmerged
as of this writing. The evidence below is static verification, `--check --diff` dry
runs, and the live apply to both Docker hosts, which converged to a clean idempotent
re-run on each. The Live-apply findings section records what only the apply could
teach.

## Guidance

### A service running without its templated `.env` is a config change waiting to happen

This is the sharpest lesson, and the one most likely to bite again.

Portainer on `n5pro_docker` had no `.env` on disk at all — `ansible -m stat` confirmed
`/data/deploy/portainer/.env` simply did not exist. The role had always templated one
to that path, so the absence was invisible until a dry run reported the template task
as `changed` on a host where every other `.env` task said `ok`.

The consequence is not "a missing file gets created." It is that **the running
container was created from different inputs than the repo believes**. With no `.env`,
`docker compose` fell back to the compose file's own default subnet — `172.23.0.0/24`
— while `host_vars` said `172.34.0.0/24`. The first deploy to successfully template
`.env` would therefore hand compose a *changed* network subnet, which recreates the
network and every container attached to it: the exact failure documented in
`docs/solutions/integration-issues/docker-compose-shared-network-subnet-recreate.md`.
It would also have moved the network to `172.34.x`, which is outside RFC1918 — public
address space on a private bridge (#84).

Note the direction. That existing doc covers a *definition* change: editing the
compose file's ipam text alters the network's config hash and forces a recreate even
when the resolved value is identical. This is the mirror image — the definition never
changed; the **resolved value** did, because a variable that had always fallen back to
its `${VAR:-DEFAULT}` default suddenly had a real value behind it. "I didn't touch the
compose file" is not evidence that the network is safe.

So: **before letting a first-ever `.env` land, inspect what the running
container/network was actually created from**, and pin the inventory to observed
reality. Renumbering is a legitimate change; it is just a *different* change, made
deliberately, with its own verification. The pin carries a comment explaining that the
value is pinned to observation rather than preference, so the next reader does not
"correct" it back (`ansible/inventory/host_vars/n5pro_docker/vars.yml:40-45`).

The general rule: a variable that has never been rendered is not a setting, it is a
hypothesis. Treat the first render as a live config change and diff it against reality
first.

### A compose project's identity is its deploy-dir basename

`docker compose` derives the project name from the directory it runs in. Rename the
directory and compose no longer recognizes the running stack — it builds a new project
and recreates the containers.

That makes a payload-preserving *repo-side* rename and a *host-side* rename two very
different operations. #85 absorbed #93 (retire the `lyrion` name), so repo-side the
role, tag, directory and compose file are all `lms`. Host-side, it still deploys to
`/data/deploy/lyrion`, and `/data/lyrion` still holds live state. The reasoning lives
at the top of `ansible/roles/services/lms/tasks/main.yml:1-6` precisely because the
inconsistency looks like an oversight and would otherwise get "fixed":

```yaml
# Naming (#93): repo-side this service is `lms` everywhere (role, tag, files).
# The HOST paths deliberately keep the legacy `lyrion` name: the deploy-dir
# basename is the compose project name, so renaming {{ data_mount }}/deploy/lyrion
# would recreate the container under a new project, and {{ data_mount }}/lyrion
# holds live state. A host-path migration would be a deliberate, separate change.
```

Renaming things repo-side is cheap. Renaming the directory compose runs in is a
migration.

### A gitleaks allowlist must outlive the file it covers

CI scans **full history**: `gitleaks git --log-opts=--all --config .gitleaks.toml
--redact --verbose` (`.github/workflows/ci.yml:51`). #85 deleted Grafana's vendored
`sample.ini`, which the allowlist existed to cover.

The obvious follow-up edit — "the file is gone, drop its allowlist rule" — turns CI
red, because the file's historical blobs are still reachable from `--all` and still
contain the flagged constant. Deleting a file removes it from HEAD, never from
history. The allowlist's `paths` regex still names the old `containers/...` path for
exactly that reason.

The rule was kept and the comment rewritten to say why, since the comment is the only
thing standing between the next reader and a red pipeline
(`.gitleaks.toml:41-45`). Verified after the deletion: a full-history scan reports `no
leaks found`. (Don't anchor on a commit count — `--log-opts=--all` follows every ref
the checkout happens to have, so the number differs between a clean CI clone and a
developer worktree carrying extra branches.)

### ansible-lint (strict) will yamllint your Docker payload

Collocating compose files *into* a role puts them inside ansible-lint's scope, where
its bundled yamllint applies Ansible YAML conventions to content that is not Ansible.
The payload needs an explicit exclusion (`ansible/.ansible-lint:30-33`):

```yaml
  # Service deploy payload (compose files + shipped configs) is Docker content,
  # not Ansible content — ansible-lint's bundled yamllint must not gate it.
  # Validated instead by scripts/validate-compose.sh (CI compose-validate job).
  - roles/services/*/files/
```

The exclusion is only defensible because something else validates that tree —
`scripts/validate-compose.sh`, run by CI. Excluding content from a linter without
naming its replacement is how coverage quietly disappears.

### Guard any task that consumes a file an earlier task ships

Renaming the compose file (`<svc>.yml` → `compose.yaml`) creates a bootstrap problem
unique to check mode: the *first* `--check` runs against a host that still has the old
filename, because `--check` does not actually ship anything. `docker_compose_v2`
cannot preview against a file that is not there, so the documented
dry-run-before-apply workflow would hard-fail on the very run meant to validate it.

This is not hypothetical — it already happened once. During #73 the observability
dry run failed for exactly this reason: check mode never synced the new compose file,
so Compose evaluated the stale on-host file and died on a `service_healthy` dependency
(session history). Fixing it in the shared role fixes it for all eleven services at
once.

The fix is a stat plus a conditional
(`ansible/roles/services/_deploy/tasks/main.yml:40-58`):

```yaml
- name: Stat deployed compose file for {{ svc }}
  ansible.builtin.stat:
    path: "{{ svc_deploy_dir }}/compose.yaml"
  check_mode: false
  register: svc_compose_stat

- name: Deploy compose stack for {{ svc }}
  community.docker.docker_compose_v2:
    project_src: "{{ svc_deploy_dir }}"
    files:
      - compose.yaml
    state: present
    pull: always
    recreate: "{{ svc_recreate }}"
  when: not ansible_check_mode or svc_compose_stat.stat.exists
```

Note `check_mode: false` on the stat — a read-only fact must be gathered for real even
in check mode, or the guard has nothing to decide on.

**This is not the stat-gate anti-pattern.** `ansible-change-loop-pitfalls.md` §2 warns
that gating an already-idempotent *verifying* module behind `when: not stat.exists`
means a corrupt on-disk file is trusted forever. The distinction is which run the gate
affects. That anti-pattern suppresses verification on **every** run; this condition is
`not ansible_check_mode or ...`, so on a real converge the module always executes and
owns its own idempotency. Only the *preview* degrades, and only when the file it would
preview does not exist yet. A gate that changes real behaviour is a bug; a gate that
only makes `--check` honest is not.

### `--checksum` decides what to transfer, not whether mtimes change

`rsync --checksum` compares content instead of mtime when deciding what to send — but
it still touches mtimes on the files it skips. A fresh clone or worktree gives every
file a new mtime, so a `synchronize` task reports `changed` with the itemization
`.f..t......`: `t` for timestamp, and crucially *no* `c` — content identical.

That is harmless until `changed` drives something. Here it drove a Grafana restart, so
a plain `git clone` and deploy would bounce Grafana for nothing, once per fresh
checkout. `times: false` makes content the only signal that can flip `changed`
(`ansible/roles/services/observability/tasks/main.yml:45-51`, and the same on the
dashboards sync at `:59-65`).

The generalization: when a task's `changed` flag triggers a side effect, audit every
attribute that can set that flag, not just the one you care about.

### The verification ladder that caught the last two

Neither hazard came from reading the diff. Both came from method:

1. **Prove payload identity mechanically, not by eye.** `git diff -M100% --summary
   origin/master...HEAD` — every moved file must appear as `rename … (100%)`. A file
   showing as delete+create means its content changed, which under a byte-identity bar
   is a defect, not a detail. Here: 38 renames at 100%, with the only non-rename lines
   being the intended creates and deletes.
2. **Write the expected deltas down before running the dry run**, then audit the
   `--check --diff` output against that list line by line. An expectation formed after
   reading the output is not a test.
3. **Scrutinize every `deleting` line.** The shared sync uses `delete: true`, which is
   what makes a file removed from the repo actually leave the host — and equally what
   makes an unexplained `deleting` line a data-loss risk. Four unexpected ones appeared
   (a `.DS_Store`, two empty stray directories, and a stale `joplin.sql` dump); each was
   investigated read-only and cleared before the apply, not after.

Steps 2 and 3 are what turned "portainer's `.env` task says changed" from a shrug into
a caught network renumber.

**One caution on step 2.** This proof bar depends on `--check --diff`, and `--diff` on
a templated `.env` prints the rendered file — secrets included. A watchtower dry run in
this repo once printed a Gmail app password in cleartext for exactly this reason
(session history). The exposure is inherent to `--diff`, not to any one template. It is
survivable here only because a task that reports `ok` prints no diff body, so only
genuinely-changing `.env` files are ever shown. When a secret-bearing `.env` *is*
expected to change, plan for `no_log` or redaction before running the dry run in a
shared or logged context.

## Live-apply findings

The dry run caught what it could. Four things only the apply could teach.

### Template comments are payload

A comment-only edit to a `.j2` that renders into a live config file is not cosmetic. It
flips the template task's `changed`, and `changed` is what downstream machinery keys
off. Here, rewording one comment line in `settings.yml.j2` — the line that used to
advertise the mirror pair this change deleted — flipped the searxng template task, which
feeds `svc_recreate: always`, which recreated the container. The recreate was correct
behaviour working exactly as designed; the surprise was that a comment triggered it.

Audit template diffs for their **consequences**, not their content. "It's only a
comment" is a statement about the diff; whether it restarts a service is a question
about what reads that task's `changed` flag.

### Creating a file in an rsync'd directory costs one idempotency cycle

Writing a new file into a directory that a later `synchronize` task also manages bumps
that directory's mtime. The *next* sync therefore reports one final `changed` while it
settles the directory's own metadata, and the run after that is clean.

This showed up as n5pro needing three runs to go quiet, where eq12 went quiet on two —
the difference being that n5pro was the host creating portainer's `.env` for the first
time. That is convergence, not non-idempotency. Before chasing a phantom idempotency
bug, check whether the previous run created a file inside a directory the run also
syncs, and simply run once more.

### Live-vs-repo drift is invisible to a collocation dry-run

Renaming the compose file means rsync **deletes** the old-named file and **sends** the
new one. It never content-compares the two, because as far as rsync is concerned they
are unrelated paths. So if the live file had drifted from the repo — hand-edited on the
host, or left behind by an older revision — the dry run shows only `deleting <old>` and
`<new>` being sent, and reports nothing about the drift it is silently resolving.

A byte-identity proof covers repo→repo. It does not cover repo→host. If an apply must be
recreate-free, diff the live files against the repo copies *before* applying; the dry run
structurally cannot do it for you.

### A fleet-wide simultaneous restart is probably your package manager

Containers restarting across both hosts within minutes of each other looks alarming and
looks like Watchtower. Check `dpkg.log` first: unattended-upgrades bumping `docker-ce`
restarts the daemon, which restarts its containers, on every host at roughly the same
time because every host runs the same upgrade timer.

Distinguishing this matters because the signatures differ in kind: a daemon upgrade
**restarts** containers (same container ID, new uptime), while Watchtower and compose
**recreate** them (new container ID). Read the container ID before reading the clock.

## Why This Matters

The unifying idea is that **a refactor's blast radius is not bounded by the files it
edits**. Every hazard here came from something the repo did not know about the host:
a `.env` that was never rendered, a directory name compose treats as identity, a
timestamp rsync updates as a side effect, a blob that outlives its file.

Under an explicit no-recreate bar these are all the same failure: state on the host was
created from inputs the repo cannot see, and the change makes the repo's version
authoritative for the first time. That is not a migration you can review your way to —
it only shows up when you compare intent against observed reality, host by host, before
applying.

The cost asymmetry is stark. Auditing a dry run costs minutes. The portainer path alone
would have recreated a network and its containers and moved a bridge into public
address space, discovered after the fact, on the host that also runs the vault.

## When to Apply

- Moving, renaming, or restructuring a directory that a deployment tool treats as
  identity — compose project dirs above all.
- Landing a templated config file (`.env`, rendered settings) on a host for the first
  time, or on a host where you cannot confirm one already exists.
- Introducing `delete: true` / `--delete` on a directory a deploy tool owns.
- Consolidating N copy-pasted blocks into one shared implementation, where a behaviour
  change now reaches every consumer at once.
- Deleting a file that a secret scanner, linter, or CI allowlist names by path.
- Any change whose acceptance bar includes "nothing restarts".

## Examples

**Pinning inventory to observed reality rather than intent**
(`ansible/inventory/host_vars/n5pro_docker/vars.yml`):

```yaml
docker_networks:
  immich: "172.31.0.0/24"
  frigate: "172.32.0.0/24"
  nextcloud: "172.33.0.0/24"
  # Pinned to the LIVE network: portainer ran without a templated .env until #85, so
  # its network was created from the compose default (172.23.0.0/24). Templating any
  # other value would force a network+container recreate (see docs/solutions/
  # integration-issues/docker-compose-shared-network-subnet-recreate.md). A deliberate
  # renumber belongs to #84.
  portainer: "172.23.0.0/24"
```

**Making content the only change signal**
(`ansible/roles/services/observability/tasks/main.yml`):

```yaml
- name: Deploy Grafana provisioning
  become: false
  ansible.posix.synchronize:
    src: "{{ role_path }}/files/data/grafana/provisioning/"
    dest: "{{ data_mount }}/grafana/provisioning/"
    delete: true
    times: false
    rsync_opts:
      - "--checksum"
  register: grafana_provisioning
```

Before: both Grafana syncs reported `changed` with `.f..t......` on every file, so the
downstream restart task would have fired. After: both report `ok`, and the play's
`changed` count dropped from 10 to 8 — exactly the two tasks.

**Reading an rsync itemization** — the columns are what let you distinguish a real
change from a metadata touch:

```
.f..t......  dashboards.yaml     # mtime only, content identical — not a real change
<f+++++++++  compose.yaml        # new file being sent
*deleting    observability.yml   # removed from repo, so removed from host
```

**Confirming payload identity across a large move:**

```bash
git diff -M100% --summary origin/master...HEAD
# every moved payload file must read: rename ... (100%)
# a delete+create pair means the content was edited — under a byte-identity bar, a defect
```

## Related

- `docs/solutions/integration-issues/docker-compose-shared-network-subnet-recreate.md`
  — the recreate mechanism the portainer pin avoids triggering (#45). Covers the
  *definition-hash* trigger; this doc covers the *resolved-value* trigger. Read both.
- `docs/solutions/integration-issues/compose-up-recreates-watchtower-created-containers.md`
  — why some containers recreate on a deploy that changed nothing about them; found
  while decomposing this change's apply.
- `docs/solutions/conventions/ansible-change-loop-pitfalls.md` — §1 check-mode safety
  and §2 existence gates; see the carve-out above for why this change's stat guard is
  not the anti-pattern §2 describes.
- `docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md` — the
  canonical casualty of the mirror-pair class. That doc fixed the instance; collocation
  removes the class.
- `docs/solutions/integration-issues/searxng-use-default-settings-and-braveapi.md` —
  documents the literal mirror pair this change deleted.
- Issue #85 (collocation), #93 (lyrion→lms naming, absorbed), #92 (Portainer-era
  residue — tracked files done here, untracked purge outstanding), #84 (network
  renumbering, deliberately deferred), #78 (postgresql configs never deployed,
  behaviour preserved unchanged), #90 (the CI this change had to keep green).
- `scripts/validate-compose.sh` — the CI gate that justifies excluding
  `roles/services/*/files/` from ansible-lint.

# CLAUDE.md

Guidance for AI assistants working in this repository. Keep it concise — this file
loads into context every session.

## Project Overview

Multi-machine homelab managed with **Ansible** (infrastructure) and **Docker Compose**
(services). Two-layer automation:

| Layer | Tool | Manages |
| ----- | ---- | ------- |
| 1. Host + VM/LXC | Ansible | Proxmox OS, repos, ZFS, GPU passthrough, VM/LXC lifecycle |
| 2. Services | Ansible + Docker Compose | Docker install, `.env` templating, compose deployment |

Everything deploys via `task deploy:full` (= `ansible-playbook playbooks/site.yml`).

## Critical Rules

1. **Change systems via Ansible, never ad-hoc SSH.** If a fix is needed on a host or
   container, encode it in the relevant role/playbook and re-run — do not `ssh` in and
   mutate state by hand. SSH is for read-only diagnostics only.
2. **Support both operator platforms.** This repo is driven from a Mac (Homebrew, zsh)
   and an Arch/Omarchy PC (`pacman`/`yay`, zsh). Any install instructions must cover both.
3. **Secrets live in ansible-vault.** Never commit plaintext secrets. `.env` files are
   templated at deploy time from `vault.yml` vars and never committed. `.vault_password`
   is gitignored.
4. **`become: false` on `synchronize` tasks.** Minimal Debian LXCs lack `sudo`; we
   connect as `root`. With `become: true`, Ansible injects `--rsync-path='sudo …'` and
   rsync fails. See [Gotchas](#gotchas).
5. **Dry-run before live.** Prefer `--check --diff` (e.g. `task infra:hosts:check`) and
   `--limit <host>` to scope changes before applying broadly.

## Repository Map

```
homelab/
├── CLAUDE.md / AGENTS.md   # this file (AGENTS.md is a symlink)
├── README.md               # human-facing overview, service tables
├── CONCEPTS.md             # shared domain vocabulary (entities, processes, status concepts)
├── Taskfile.yml            # task runner — see Key Commands
├── docs/                   # architecture.md, onboarding.md, eq12.md, n5pro.md, ups.md
│   └── solutions/          # documented fixes to past problems, by category, with YAML frontmatter (module, tags, problem_type)
└── ansible/
    ├── inventory/          # hosts.yml, group_vars/, host_vars/ (+ vault.yml)
    ├── playbooks/          # site.yml, proxmox-hosts.yml, configure-guests.yml, deploy-services.yml
    └── roles/              # common, proxmox_host, proxmox_guests, docker_host, nut, services/* (per-service files/compose.yaml + templates/env.j2)
```

## Key Commands

```bash
task deploy:full          # full pipeline: hosts → guests → services
task infra:hosts          # 1. Proxmox OS config + VM/LXC provisioning
task infra:guests         # 2. Docker + base packages inside guests
task deploy:services      # 3. deploy all compose stacks
task deploy:service -- --tags <svc>   # deploy one service (e.g. --tags postgresql)
task infra:hosts:check    # dry-run host config (--check --diff)
task ansible:lint         # ansible-lint
task ansible:ping         # connectivity check
task vault:edit -- <path> # edit an encrypted vault file
```

Scope any command to one host with `-- --limit <host>` (e.g. `task infra:hosts -- --limit eq12`).

## Making a Change

1. Edit the relevant role (`ansible/roles/…`). Compose stacks live at `ansible/roles/services/<svc>/files/compose.yaml`; per-service deploy logic is the shared `services/_deploy` role.
2. Dry-run: `task <cmd> -- --check --diff --limit <host>`.
3. Apply, scoped: `task <cmd> -- --limit <host>`.
4. Verify (read-only SSH is fine): `ssh root@<host> 'docker ps'` or hit the service.
5. Service placement is controlled by the `services` list in
   `ansible/inventory/host_vars/<host>/vars.yml`.

## Architecture Quick Reference

| Host | Proxmox node | IP | Hardware | Key guests |
| ---- | ------------ | -- | -------- | ---------- |
| Beelink EQ12 | `pve` | 192.168.25.5 | Intel N100, 16GB | HA VM, deb-docker (CT 101), NPM |
| Minisforum N5 Pro | `n5pro` | 192.168.30.5 | Ryzen AI 9 HX 370, 96GB | TrueNAS VM (200), n5pro-docker (CT 201) |

- Docker networks use `172.x.x.x` to avoid LAN (`192.168.x.x`) conflicts.
- N5 Pro: GPU `/dev/dri` device-shared into CT-201 (VAAPI for Frigate/Immich);
  TrueNAS VM gets full PCI passthrough of SATA + NVMe.
- NFS: n5pro-docker → TrueNAS over host-only `vmbr2` (`10.99.99.x`).

Full topology, port maps, and network tables: **[docs/architecture.md](docs/architecture.md)**.
Per-machine detail: [docs/eq12.md](docs/eq12.md), [docs/n5pro.md](docs/n5pro.md).

## Gotchas

Hard-won lessons — check here before debugging from scratch.

- **Docker-in-LXC + AppArmor.** Docker inside a privileged LXC detects AppArmor in the
  kernel and tries to load its `docker-default` profile, failing with
  `apparmor_parser: Access denied`. Fixes (both applied): the `docker_host` role masks
  AppArmor *before* configuring/starting Docker, and every compose service sets
  `security_opt: ["apparmor:unconfined"]`. New services must include this. The **mask**
  is the privileged-CT half of the fix and is applied only where `docker_lxc: true`
  (n5pro_docker, CT 201); unprivileged CT 101 does not see the host's AppArmor and
  needs no mask — don't "fix" eq12 by adding one. The per-service `security_opt` is
  fleet-wide regardless, and #95 added `no-new-privileges:true` alongside it (in-container
  escalation defense matters more here precisely because AppArmor is unconfined) — new
  services need both lines. **One documented exception, and it is the shape of the trap:**
  `no_new_privs` strips setuid/fscaps at `execve`, so telegraf silently lost `/usr/bin/ping`
  — eight ping metrics went to NO DATA while the container stayed healthy. Any container
  whose function depends on a setuid binary needs the same carve-out (record the failed
  alternatives beside it; for telegraf, `ping_group_range` cannot work under the LXC userns
  map and its native pinger still needs raw sockets). After adding the line, verify the
  app's own OUTPUT, not just that it started.
- **AppArmor 4.1 / PVE 9 ABI regression (host profiles).** PVE 9's AppArmor 4.1 default
  ABI enforces fine-grained `AF_UNIX` mediation that older bundled profiles (e.g.
  `dhclient`) predate, so unix sockets are denied (`failed protocol match`) — flooding the
  console on DHCP hosts. Fix is an ABI pin (`abi <abi/3.0>,`) in the profile **preamble**
  (inert inside the block / a local include) plus a `network unix dgram,` grant. Test
  deterministically with `aa-exec -p <profile> -- python3 -c 'socket(AF_UNIX,SOCK_DGRAM)'`.
  Full runbook: [docs/solutions/integration-issues/proxmox-dhclient-apparmor-af-unix-denial.md](docs/solutions/integration-issues/proxmox-dhclient-apparmor-af-unix-denial.md).
- **Proxmox privileged LXC feature flags.** Setting flags like `nesting=1` on a
  *privileged* container via API token returns `403 Forbidden` — Proxmox requires a
  `root@pam` password session. Workaround: omit `features` in the `community.proxmox.proxmox`
  call for privileged CTs, then apply via `ansible.builtin.shell: pct set {{ vmid }} -features …`
  over SSH as root.
- **`synchronize` + sudo.** Always `become: false` on `ansible.posix.synchronize` tasks
  (see [Critical Rules](#critical-rules)).
- **Bind-mounted config must be owned by Ansible — and actually wired.** If a compose file
  bind-mounts a config path, the role must *deploy the file*, not just `mkdir` the directory —
  otherwise the repo copy is decorative and edits silently do nothing. Deploying is still not
  wiring: postgres ignored its correctly-mounted `pg_hba.conf` for the deployment's whole life
  because nothing set `hba_file` (the default is `$PGDATA/pg_hba.conf`). Verify with the app's
  own introspection (`SHOW hba_file;`, `nginx -T`), never `docker inspect`. Compose ignores
  bind-mount content changes, so pair the copy with an explicit restart — but never restart a
  container the same run created (it races first-boot init; postgres then skips
  `/docker-entrypoint-initdb.d` forever), and follow the restart with a readiness gate
  (`pg_isready`): modules return on issuance, not on service. Related: never
  `depends_on: condition: service_healthy` a scratch/distroless image — a healthcheck needs a
  binary inside the image, so one that can never pass permanently blocks every redeploy.
  See [docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md](docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md)
  and [docs/solutions/integration-issues/postgresql-mounted-configs-never-deployed-or-read.md](docs/solutions/integration-issues/postgresql-mounted-configs-never-deployed-or-read.md).
- **Watchtower scan scope & update posture.** With `WATCHTOWER_LABEL_ENABLE=true`,
  `watchtower.enable=true` is what puts a container in scope; `monitor-only=true` on its own is
  inert and the container is never scanned or reported. Every new compose service MUST pick a
  posture class (#83): stateless/restart-tolerant → `enable` only (auto-update); stateful,
  schema-migrating, or socket-holding → `enable` + `monitor-only` (notified, updated only via a
  deliberate deploy — the pipeline's `pull: always` is the update mechanism). Decision table:
  `ansible/roles/services/watchtower/README.md`. Verify by comparing `scanned=N` against the
  labelled count — wait-free via a `--run-once --monitor-only` probe, which on our Docker-in-LXC
  hosts requires `--security-opt apparmor=unconfined`. Pinning any image to an immutable tag
  opts it out of ALL watchtower notifications (it only checks the reference the container runs)
  — watchtower itself is pinned this way, so its bumps are a manual check of
  <https://github.com/nicholas-fedor/watchtower/releases> (GitHub org ≠ Docker Hub namespace
  `nickfedor`; the lookalike GitHub URL 404s — don't "fix" it).
  See [docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md](docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md).
- **nftables `hook input` is inert for docker-published ports.** Docker DNATs published
  ports in prerouting (`dstnat`/-100) and the traffic takes the FORWARD path — an
  input-hook allowlist loads cleanly and filters nothing. Filter docker-published ports
  in `hook prerouting` at a priority before -100 (we use -150) and scope with
  `fib daddr type != local accept` or you silently drop container egress to the same
  port elsewhere. Also: a `RemainAfterExit` oneshot that loads an nft table stays
  "active" after the table is externally flushed, so `state: started` can never heal
  it — probe the kernel (`nft list table`), heal via handler + `flush_handlers`, and
  hard-assert. Verify from a BLOCKED source, not just an allowed one.
  See [docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md](docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md).
- **Ansible variable precedence.** Inventory `group_vars` REPLACE role defaults (lists never
  merge). Role-critical packages go in `roles/<role>/vars/main.yml`, which outranks inventory.
  See [docs/solutions/security-issues/unattended-upgrades-silently-inert-fleet-wide.md](docs/solutions/security-issues/unattended-upgrades-silently-inert-fleet-wide.md).
- **ISO/large downloads.** Use `get_url` with a `checksum:` and NO stat-exists gate — the
  module hashes the on-disk file, skips the fetch when it matches, and re-downloads on
  mismatch. A `when: not <stat>.stat.exists` guard defeats that verification: a corrupt
  file already on disk is trusted forever. Pin version + sha256 in ONE place and bump
  deliberately (TrueNAS ISO: defined in `proxmox_guests/defaults/main.yml`; the filename is
  consumed by `n5pro` host_vars' `ide2`). See
  [docs/solutions/conventions/ansible-change-loop-pitfalls.md](docs/solutions/conventions/ansible-change-loop-pitfalls.md).
- **macOS Local Network privacy (control machine).** On macOS, the Python that Ansible
  runs (e.g. the `uv`-managed `ansible-core`) needs Local Network permission to reach
  `192.168.x.x` Proxmox APIs — symptom is `[Errno 65] No route to host` from Ansible while
  `curl`/`ping` work. Grant it in System Settings → Privacy & Security → Local Network.
- **Compose project identity = deploy-dir basename.** Renaming a deploy dir (or a service) recreates its containers under a new project. lms still deploys to `/data/deploy/lyrion` for exactly this reason — see the comment in `roles/services/lms/tasks/main.yml`.
- **`compose up` recreates any container Watchtower last created**, even with a byte-identical compose file — so the first deploy after a Watchtower update bounces that service, and a recreate in a deploy is not proof your change caused it. See [docs/solutions/integration-issues/compose-up-recreates-watchtower-created-containers.md](docs/solutions/integration-issues/compose-up-recreates-watchtower-created-containers.md).
- **A service running without its templated `.env` is a config change waiting to happen.** Portainer on n5pro had no `.env` on disk, so its docker network was created from the compose file's *default* subnet, not host_vars. The first deploy that templates `.env` therefore changes the subnet and recreates the network + container. Before letting a first-ever `.env` land, check what the running container/network was actually created from (`docker network inspect`) and pin host_vars to that. The deferred renumber has since happened (#84): n5pro's pins and its `docker_default_address_pools` override now live in the documented fleet map in `ansible/inventory/host_vars/n5pro_docker/vars.yml`. A new service takes its next free subnet from that map — role `env.j2` subnet vars are `mandatory()` and fail the template rather than falling back.

- **`community.proxmox` LXC module updates by default — pin `update: false`.** Since
  community.proxmox 1.0.0, `community.proxmox.proxmox` defaults `update: true`: `state: present`
  on an *existing* CT diffs task params against stored config and, on ANY mismatch, PUTs the
  **full kwargs set** (netif/rootfs/mp included), reporting `changed` unconditionally. Several
  params can never converge (`tags` compared as Python list-repr, URL-encoded `description`,
  allocation-form `disk` vs the real allocated subvol), so every run becomes a blind config PUT —
  against a running CT it lands in a `[pve:pending]` section. `proxmox_kvm` still defaults false;
  both provisioning calls in `proxmox_guests` pin `update: false` (creation only — all updates are
  explicit reconcile tasks; do not remove the pins). Guest resource changes: `pct set`
  cores/memory is hot for CTs; VM cores/memory apply only to a confirmed-stopped VM (no
  memory/cpu hotplug enabled) — the role fails loudly otherwise.
  See [docs/solutions/integration-issues/community-proxmox-update-default-blind-config-put.md](docs/solutions/integration-issues/community-proxmox-update-default-blind-config-put.md).
- **LXC `features` has no `nfs` key, and shell tasks need `set -e`.** Valid feature keys are
  exactly `mount, nesting, keyctl, fuse, mknod, force_rw_sys` (PVE `$features_desc`); "may mount
  NFS" is spelled `mount=nfs`. The features task asked for `nfs=1` for the deployment's whole
  life — `pct set` rejected it every run, and the script (no `set -e`) swallowed the error and
  echoed "changed": a silent-green no-op. Also compare against `pct config` — never the raw
  `/etc/pve/lxc/<id>.conf`, whose snapshot sections carry their own `features:` lines and
  false-match greps. But `pct config` is **not** the "live view": it prints the PENDING-merged
  config, so any "is it actually applied?" question needs `pct config <vmid> --current` (CT 201
  reads `mount=nfs,nesting=1` plain vs `nesting=1` under `--current`). And never `lineinfile`
  /`replace` into those raw files — a regexp matches the **last** occurrence, which is a
  snapshot's stale copy, not the live section (#98).
  See [docs/solutions/integration-issues/lxc-features-nfs-invalid-key-silent-green.md](docs/solutions/integration-issues/lxc-features-nfs-invalid-key-silent-green.md)
  and [docs/solutions/integration-issues/lineinfile-last-match-edits-lxc-snapshot-not-live-config.md](docs/solutions/integration-issues/lineinfile-last-match-edits-lxc-snapshot-not-live-config.md).
- **Create-time-only guest fields are REBUILD declarations — write them in the
  syntax a fresh create needs.** With provisioning pinned `update: false` (#86),
  `mounts`/`scsi`/`efidisk0`/`usb`/etc. in host_vars never touch an existing guest;
  their only consumer is a from-scratch rebuild. A named subvol
  (`local-zfs:subvol-101-disk-1`) is therefore a DR landmine — the volume doesn't
  exist on a rebuilt host and creation fails; use allocation form
  (`local-zfs:110,mp=/data`) and keep the data-restore note beside it (no enforced
  restore gate yet — #127). Pin the rest of the model to the live guest
  (`qm config`/`pct config`, dated in the comment): VM 100 ran
  i440fx + efidisk0 + USB sticks + pinned MAC while vars said q35-and-nothing-else.
  Guest-definition keys are schema-checked against the role's read surface — adding
  a key means teaching the role to read it AND adding it to the allowlist in the
  same change. Editing VM 100's `net0` string later hot-rewrites the running HA
  VM's NIC (it is live-reconciled now).
  Two rebuild-only traps measured 2026-08-21 (#157): `pveam available | grep <os> | tail -1`
  picked **arm64** once upstream published both arches (it sorts after `amd64`) — the CT
  creates fine and dies at boot with `Exec format error`; select on the arch COLUMN
  (`awk '$3 == arch'`) and map `ansible_architecture` with NO `| default('amd64')`. And
  `resolved_templates[item.os] | default(...)` only catches an **Undefined**: on a host
  mixing `os:` and `ostemplate:` guests the lookup is a real dict, so `item.os` raises
  `dict has no attribute 'os'` and fails the whole play on EVERY run. `| default()` is not a
  membership test — use `if item.os is defined`.
  See [docs/solutions/integration-issues/create-time-only-fields-are-rebuild-declarations.md](docs/solutions/integration-issues/create-time-only-fields-are-rebuild-declarations.md).
- **`authorized_key` reports ok while deploying ZERO keys.** It strips blank and
  `#`-comment lines before parsing, so an empty or comment-only `.pub` (truncated
  copy, `touch`ed placeholder) sails through a file-count guard — and the next
  task in `common` writes `PasswordAuthentication no`. The role therefore asserts,
  before hardening: files exist, each file contains a real key
  (`(?m)^(ssh-…|ecdsa-sha2-|sk-)`), and the deploy task's results are non-empty —
  the whole chain tagged `ssh_hardening` so a `--tags` run can't bypass it. On an
  already-hardened host a failing assert never un-hardens (keys and drop-in
  persist). Fresh-guest configuration also waits for SSH now
  (`configure-guests.yml` pre_tasks, `tags: always` — play-level `gather_facts`
  used to run under any `--tags`, an ordinary `setup:` task does not).
- **Compose dotenv interpolates `$` in unquoted `.env` values — and secret-shaped traps
  compound.** A templated `.env` value containing `$` (Argon2 PHC hashes, rotated
  passwords) is silently truncated unless single-quoted (`ADMIN_TOKEN='...'`); the
  compose file's `${VAR}` substitution is single-pass, so quoting in `.env` is
  sufficient. Related: vaultwarden accepts a non-PHC `ADMIN_TOKEN` as a *plaintext*
  token instead of rejecting it — assert the `$argon2id$` shape in the role
  (see `roles/services/vaultwarden/tasks/main.yml`). Secret-bearing template tasks set
  `diff: false` (`_deploy` .env, NUT configs, searxng
  settings.yml — #88; postgres' vaulted init script already had it from #78): the file
  still reports changed, its content never renders — give every new secret-bearing
  template/copy task the same treatment. Reserve `no_log: true` for secrets in module
  *args* (`proxmox_guests` precedent); no_log also censors failure output, which would
  blind a fail-loudly assert (#88 asserts VM/VL/Grafana creds non-empty BY NAME, with
  compose `${VAR:?}` as backstop). Fleet-wide
  quoting sweep: #117. **Sibling trap in `compose.yaml` itself:** an unquoted YAML scalar
  ends at a ` #`, so `GF_SMTP_USER: ${VAR:?required - see #139}` reaches compose as
  `${VAR:?required - see` and fails the parse of the WHOLE file with "invalid
  interpolation format". A `#` after a non-space survives (`(#143)` does) — quote the
  value rather than relying on that. Measured: it failed the first live apply of #139. See
  [docs/solutions/security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md](docs/solutions/security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md).

- **A guard you have not seen fail is not a guard.** `failed_when: false` does not "let
  the result through" — it *assigns* `failed: False`, so a paired
  `assert: <reg> is not failed` is literally `assert: true`. Measured against a `wait_for`
  that timed out: `failed_when: false` → `failed=False` (assert passes);
  `ignore_errors: true` → `failed=True` (assert fires). One such guard sat inert in this
  repo for its whole life while claiming to prove the host log stream was live. Use
  `ignore_errors` + `register` when an assert must read `failed`, and **verify every new
  guard against the live defect before trusting it**. Two corollaries. (1) The test of a
  guard is the **SECOND** run, on an already-converged host — a first apply is the one run
  where even a broken guard appears to work. (2) A task whose inputs only exist *after* a
  real apply is invisible to `--check`, so its first execution is its first test; verify
  those against the binary on a host, never against a dry run (#134 shipped
  `vector validate --config`, which the CLI rejects — the path is positional — and no
  dry-run could have caught it).
- **Arm a guard from durable state, not a one-shot `changed`.** #127's restore gate keyed
  its marker task on the provisioning result's `.changed` — a signal any run that dies
  before the marker consumes for good; later runs see `changed=false` on an existing CT and
  the gate stays unarmed for exactly the rebuild it guards. Measured: kill the play after
  provisioning, re-run to a GREEN recap → running CT, empty `/data`, no markers. Fix: a
  write-ahead intent file on the host (`fresh_allocation_intent_dir`), written before the
  create, removed only after the artefacts land. Don't probe the volume instead — empty
  `/data` + no manifest can't tell an un-armed allocation from an operator-ACKNOWLEDGED one,
  so probing re-arms what the operator just cleared, in a loop (#148).
- **Absence of incidental traffic is not evidence of anything — measure the empty-window
  fraction, then make the signal deliberate.** Host logs here arrive in **bursts**:
  measured 2026-08-20, 66% of eq12_docker's and 40% of eq12's 10-minute windows were
  legitimately empty in perfect health, while n5pro never had one. Burstiness does **not**
  track volume — the busiest host was the worst offender. Gap percentiles actively mislead
  (eq12_docker: p99 gap 15.6s, yet two-thirds of windows empty), so the only
  decision-relevant statistics are **max gap** and **empty-window fraction**. Widening a
  window is not a fix: a max gap of *exactly* 3600.0s is one incidental hourly event
  holding it open, not headroom. The fix is a heartbeat (`roles/rsyslog_structured`, one
  marker per host per 5 min) so absence becomes a fact. #134 made this same category error
  **twice** — an alert rule that paged every ~30 min for 21 h, and a deploy assert
  demanding container traffic from three silent containers — and the second was found only
  because the first taught us to look. **A caveat in an error string is where a missing
  conditional hides:** the broken assert's own `fail_msg` named the false-alarm case and
  asserted anyway.
  Alerting corollary (#151): a counter that only EXISTS while non-zero
  (`vector_component_errors_total`) can't carry `noDataState: Alerting` — absence IS its
  healthy state. Give those `noDataState: OK` and put liveness on a separate,
  continuously-exported series (`min(lag(vector_uptime_seconds[24h]))`).
- **`--check` overstates `docker_compose_v2` churn: a predicted `Recreate` is not a real
  one.** Dry-runs of `services/*` roles routinely report `Pulling` + `Recreate` for
  containers a real run then leaves untouched (`ok`, same container ID, same `.Created`).
  Do not abandon a change over a check-mode recreate — and do not accept one as proof your
  change caused a bounce. Diff the dry-run against the SAME dry-run on master (stash the
  branch; judge the **delta**, not the absolute), then confirm after the fact with
  container ID + `.Created`, and for anything templated the file's mtime plus the unit's
  `ActiveEnterTimestamp` (a `template` that renders identical bytes leaves mtime alone —
  an mtime older than the apply is proof it never rewrote). Real recreates do happen — a
  Watchtower-created container is recreated by the next `compose up`
  ([compose-up-recreates-watchtower-created-containers](docs/solutions/integration-issues/compose-up-recreates-watchtower-created-containers.md))
  — which is exactly why the delta, not the prediction, is the evidence.
- **A backup is not verified until a RESTORE is, and a restore is not verified by "the
  database is there."** A partial restore leaves a database that exists with the right
  owner and zero tables, and psql's default is to continue past errors and exit 0 — so
  `-v ON_ERROR_STOP=1` plus an object count (tables/indexes/rows, both sides) is the check;
  `pg_database_size` legitimately differs. Re-run the drill after every PostgreSQL MAJOR
  upgrade, not just after backup-code changes: PG18 wraps dump sections in nested
  `\restrict`/`\unrestrict`, which silently invalidated our slice recipe. Marker checks are
  version- and tool-specific too — `pg_dump` ends `-- PostgreSQL database dump complete`
  while `pg_dumpall` ends `… cluster dump complete` and contains BOTH, and PG18 writes
  `\unrestrict` AFTER the marker, so grep the exact string in a bounded region and never
  assert it is the last line. Also: **never size a dump from `pg_database_size`, and never
  compare two dumps taken hours apart** — template1 is 7750 kB on disk and 720 bytes
  dumped, and a live DB's own growth (~62 KB/h here) dwarfs most format changes. See
  [docs/solutions/integration-issues/pg18-restrict-slicing-silent-green-restore-drill.md](docs/solutions/integration-issues/pg18-restrict-slicing-silent-green-restore-drill.md)
  and [docs/solutions/conventions/prove-notification-delivery-not-just-config-validity.md](docs/solutions/conventions/prove-notification-delivery-not-just-config-validity.md).

- **Measure the baseline before claiming a win, and carry verification through the
  transform.** #147's case for narrowing the Joplin dump was measured against the wrong
  baseline — narrowing was size-NEUTRAL, `gzip -1` was the lever. And verify the dump BEFORE
  compressing, fronting restore recipes with `gzip -t &&`: decompressing an unverified
  archive proves nothing about the dump inside. See
  [docs/solutions/conventions/measure-the-baseline-then-verify-before-transforming.md](docs/solutions/conventions/measure-the-baseline-then-verify-before-transforming.md).

- **A provisioned Grafana datasource is FROZEN unless its `version:` INCREASES — so a
  rotated credential never reaches it.** Grafana re-applies a provisioned datasource only
  when the file's `version` exceeds the stored one; pinned, `secureJsonData` is never
  rewritten. Measured: rotating `vault_vm_auth_password` left Grafana querying
  VictoriaMetrics with the OLD secret while the container env, the `.env` file and an
  in-container `curl -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD"` ALL showed the NEW one —
  every process-level check passed, every VM-backed rule 401'd, and the alerting stack
  could not report its own outage because the reporter was the thing that was out. Bump
  `version` in the same change as any credential. `/api/health` proves only that the
  PROCESS is up; the real check is `/api/datasources/uid/<uid>/health` (returns
  `status=ERROR, got response code 401`), which `services/observability` now asserts for
  every uid read out of the provisioning file itself.

## Conventions

- **Ansible is the only IaC** — no Pulumi/Terraform.
- **SSH keys**: drop `*.pub` in `ansible/files/ssh_keys/` (`<user>@<host>.pub`); the
  `common` role deploys all keys to every host on the next run.
- **`proxmox_bridge` is required** in every Proxmox host's host_vars (no vmbr0
  fallback since #87) — guest `net`/`net0` definitions resolve their bridge from it.
- **YAML style**: 2-space indent, `---` document start, quote strings only when required.

## Further Reading

- **[README.md](README.md)** — service inventory, ports, secrets strategy.
- **[docs/onboarding.md](docs/onboarding.md)** — migrating live infra under Ansible without
  downtime; Ansible glossary for newcomers. (Its Portainer-migration step is historical.)
- **[docs/architecture.md](docs/architecture.md)** — full network/port/topology reference.

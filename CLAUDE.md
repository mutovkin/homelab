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
├── ONBOARDING.md           # bring existing infra under Ansible safely
├── CONCEPTS.md             # shared domain vocabulary (entities, processes, status concepts)
├── Taskfile.yml            # task runner — see Key Commands
├── docs/                   # architecture.md, eq12.md, n5pro.md, ups.md
│   └── solutions/          # documented fixes to past problems, by category, with YAML frontmatter (module, tags, problem_type)
├── ansible/
│   ├── inventory/          # hosts.yml, group_vars/, host_vars/ (+ vault.yml)
│   ├── playbooks/          # site.yml, proxmox-hosts.yml, configure-guests.yml, deploy-services.yml
│   └── roles/              # common, proxmox_host, proxmox_guests, docker_host, nut, services/*
└── containers/             # Docker Compose stacks (one dir per service, standalone-usable)
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

1. Edit the relevant role (`ansible/roles/…`) or compose file (`containers/…`).
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
  `security_opt: ["apparmor:unconfined"]`. New services must include this.
- **Proxmox privileged LXC feature flags.** Setting flags like `nesting=1` on a
  *privileged* container via API token returns `403 Forbidden` — Proxmox requires a
  `root@pam` password session. Workaround: omit `features` in the `community.proxmox.proxmox`
  call for privileged CTs, then apply via `ansible.builtin.shell: pct set {{ vmid }} -features …`
  over SSH as root.
- **`synchronize` + sudo.** Always `become: false` on `ansible.posix.synchronize` tasks
  (see [Critical Rules](#critical-rules)).
- **ISO/large downloads.** `get_url` can re-validate against the server even when the
  file exists, failing if the upstream version was pulled. Guard downloads with an explicit
  `stat` check and `when: not <stat>.stat.exists`. Pin versions in one place; bump
  deliberately (e.g. TrueNAS ISO version lives in `proxmox_guests` + `n5pro` host_vars).
- **macOS Local Network privacy (control machine).** On macOS, the Python that Ansible
  runs (e.g. the `uv`-managed `ansible-core`) needs Local Network permission to reach
  `192.168.x.x` Proxmox APIs — symptom is `[Errno 65] No route to host` from Ansible while
  `curl`/`ping` work. Grant it in System Settings → Privacy & Security → Local Network.

## Conventions

- **Ansible is the only IaC** — no Pulumi/Terraform.
- **SSH keys**: drop `*.pub` in `ansible/files/ssh_keys/` (`<user>@<host>.pub`); the
  `common` role deploys all keys to every host on the next run.
- **YAML style**: 2-space indent, `---` document start, quote strings only when required.
- Architecture decisions: `docs/decisions.md`.

## Further Reading

- **[README.md](README.md)** — service inventory, ports, secrets strategy.
- **[ONBOARDING.md](ONBOARDING.md)** — migrating live infra under Ansible without downtime;
  Ansible glossary for newcomers.
- **[docs/architecture.md](docs/architecture.md)** — full network/port/topology reference.

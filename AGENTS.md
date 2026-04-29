---
description: "Always-on project preferences for the homelab repository."
applyTo: "**"
---

# AGENTS.md

## Repository

- **Name**: homelab
- **Purpose**: Multi-machine homelab automation — Ansible + Docker Compose
- **Branch**: `master` (stable), `feature/*` (new improvements)

## Operator Machines

This repo is operated from two control machines. All docs, scripts, and instructions must support both:

| Machine          | OS               | Package Manager  | Shell |
| ---------------- | ---------------- | ---------------- | ----- |
| Mac (macOS)      | macOS (Homebrew) | `brew`           | zsh   |
| PC (Omarchy 3.6) | Arch Linux       | `pacman` / `yay` | zsh   |

When writing installation instructions, always provide commands for **both** platforms.

## SSH Keys

Public SSH keys are stored in `ansible/files/ssh_keys/` (one file per key, `*.pub`). The `common` role automatically provisions all keys to every managed host. When a new key is added, it deploys on the next playbook run.

Naming convention: `<user>@<hostname>.pub` (e.g., `surge@macbook.pub`, `surge@omarchy.pub`).

## Conventions

- **Ansible** is the sole automation tool — no Pulumi, Terraform, or other IaC
- **Docker Compose** defines services in `containers/`; Ansible deploys them
- **ansible-vault** encrypts all secrets; `.vault_password` is gitignored
- **go-task** (`Taskfile.yml`) wraps common commands
- Docs live in `docs/`; architecture decisions in `docs/decisions.md`
- YAML style: 2-space indent, `---` document start, quoted strings only when required
- **Memory: Ansible Synchronize vs Sudo**: When using the `ansible.posix.synchronize` module, if the playbook runs with `become: true`, Ansible injects `--rsync-path='sudo -u root rsync'` into the rsync command. Because our minimal Debian LXC containers (e.g., `deb-docker`) do not have `sudo` installed by default, and we connect as `root` anyway, this causes rsync to fail. To bypass this, **always set `become: false` on `synchronize` tasks** (since `ansible_user: root` naturally has permissions).
- **Memory: Proxmox Privileged LXC Features**: The Proxmox API restricts setting feature flags (like `nesting=1`) on **privileged** containers when authenticating via API tokens, resulting in a `403 Forbidden` error. Even with privilege separation disabled, Proxmox requires a `root@pam` password session. To work around this in Ansible, conditionally omit the `features` argument in the `community.proxmox.proxmox` module for privileged containers. Instead, apply the features in a subsequent task using `ansible.builtin.shell: pct set {{ vmid }} -features ...` which runs natively on the Proxmox host over SSH as root.

---
description: "Always-on project preferences for the homelab repository."
applyTo: "**"
---

# AGENTS.md

## Repository

- **Name**: homelab
- **Purpose**: Multi-machine homelab automation — Ansible + Docker Compose
- **Branch**: `n5` (active development), `master` (stable)

## Operator Machines

This repo is operated from two control machines. All docs, scripts, and instructions must support both:

| Machine | OS | Package Manager | Shell |
|---|---|---|---|
| Mac (macOS) | macOS (Homebrew) | `brew` | zsh |
| PC (Omarchy 3.6) | Arch Linux | `pacman` / `yay` | zsh |

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

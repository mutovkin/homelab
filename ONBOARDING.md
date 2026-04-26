# Onboarding Guide

How to bring your existing, running homelab infrastructure under Ansible
management **without losing data or disrupting running services**.

> **The golden rule**: Every step in this guide is read-only or additive by default.
> Nothing deletes containers, VMs, volumes, or databases. If a command says "destroy"
> or "recreate", it is explicitly called out with a ⚠️ warning.

---

## Table of Contents

1. [Concepts: What Ansible Does](#1-concepts-what-ansible-does)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Ansible: Install Tools on Your Mac](#step-1--ansible-install-tools-on-your-mac)
4. [Step 2 — Ansible: Test Connectivity](#step-2--ansible-test-connectivity)
5. [Step 3 — Ansible: Encrypt Your Secrets](#step-3--ansible-encrypt-your-secrets)
6. [Step 4 — Ansible: Dry-Run Host Configuration](#step-4--ansible-dry-run-host-configuration)
7. [Step 5 — Migrate Stacks from Portainer to Ansible](#step-5--migrate-stacks-from-portainer-to-ansible)
8. [Step 6 — Ansible: Dry-Run Service Deployment](#step-6--ansible-dry-run-service-deployment)
9. [Step 7 — Go Live](#step-7--go-live)
10. [Rollback & Recovery](#rollback--recovery)
11. [Glossary](#glossary)

---

## 1. Concepts: What Ansible Does

Ansible is a **configuration management** and **automation** tool. You describe what
packages should be installed, what files should exist, what VMs/LXCs should be
provisioned, and what services should be running — Ansible makes it so via SSH and
the Proxmox API.

**Key concepts:**

| Term                       | What it means |
|----------------------------|---------------|
| **Inventory**              | The list of machines Ansible manages (`ansible/inventory/hosts.yml`). Groups machines by role (e.g., `proxmox_hosts`, `docker_hosts`). |
| **Playbook**               | A YAML file describing a sequence of tasks to run on a set of hosts. Like a recipe. |
| **Role**                   | A reusable bundle of tasks, templates, and defaults. Our roles live in `ansible/roles/`. |
| **Task**                   | A single action: install a package, copy a file, start a service, provision a VM, deploy a Docker stack. |
| **Handler**                | A task that only runs when *notified* — e.g., restart Docker only if its config changed. |
| **Idempotent**             | Running the same playbook twice produces the same result. If a package is already installed, Ansible skips it. |
| **Check mode** (`--check`) | Dry-run mode. Ansible shows what it *would* change without actually changing anything. Always safe. |
| **Diff** (`--diff`)        | Shows the exact line-by-line differences Ansible would apply. Combine with `--check` for a safe preview. |
| **Vault**                  | Ansible's built-in encryption for secrets. Passwords never appear in plain text in Git. |
| **Tags**                   | Labels on tasks that let you run only a subset. E.g., `--tags postgresql` runs only PostgreSQL deployment. |

### How Ansible Manages Everything

```ascii
┌─────────────────────────────────────────────────────┐
│  1. Ansible: Configure Proxmox hosts                │
│     (packages, networking, ZFS, GPU passthrough)    │
│                                                     │
│  2. Ansible: Provision VMs and LXC containers       │
│     (via community.general.proxmox_kvm/proxmox)     │
│                                                     │
│  3. Ansible: Configure guests (Docker, packages)    │
│     (inside the VMs/LXCs provisioned above)         │
│                                                     │
│  4. Ansible: Deploy Docker Compose services         │
│     (syncs compose files, templates .env, runs up)  │
└─────────────────────────────────────────────────────┘
```

All four steps run via a single command: `task deploy:full` (or `ansible-playbook playbooks/site.yml`).

---

## 2. Prerequisites

Install these on your Mac before starting:

```bash
# Homebrew packages
brew install ansible go-task

# Verify versions
ansible --version    # 2.15+
task --version       # 3.x
```

You also need:

- **SSH access**: You should already be able to `ssh root@pve.lan` and `ssh root@deb-docker.lan`
- **A Proxmox API token** (created during secrets setup — needed for VM/LXC provisioning)

---

## Step 1 — Ansible: Install Tools on Your Mac

```bash
# From the repo root
cd ansible

# Install required Ansible collections (community.docker, etc.)
# This downloads modules Ansible needs — does NOT touch your servers.
ansible-galaxy install -r requirements.yml

# Or use the Taskfile shortcut from repo root:
cd ..
task ansible:galaxy
```

**What just happened?** Ansible Galaxy downloaded three collection packages to your
Mac's `~/.ansible/collections/`. Nothing was sent to any server.

---

## Step 2 — Ansible: Test Connectivity

Before doing anything, verify Ansible can reach your machines:

```bash
# From repo root
task ansible:ping
```

This runs `ansible all -m ping`, which SSH's into each host in the inventory and runs
a trivial "are you there?" check. Expected output:

```
eq12 | SUCCESS => {
    "ping": "pong"
}
n5pro | SUCCESS => {
    "ping": "pong"
}
eq12_docker | SUCCESS => {
    "ping": "pong"
}
```

**If it fails:**

- Check that the IPs in `ansible/inventory/hosts.yml` are correct
- Check that `ssh root@pve.lan` works manually
- Check that `host_key_checking = False` is set in `ansible/ansible.cfg` (it is)

---

## Step 3 — Ansible: Encrypt Your Secrets

This step creates the encrypted vault files that hold your actual passwords. The
vault files are committed to Git, but their contents are encrypted with a password
only you know.

### 3a. Create the vault password file

```bash
# This file holds the single password that unlocks all vault files.
# It MUST NOT be committed to Git (.gitignore already excludes it).
echo "your-vault-password-here" > .vault_password
chmod 600 .vault_password
```

Pick a strong password and store it in your Bitwarden vault for safekeeping.

### 3b. Create the real encrypted vault for EQ12

The file `ansible/inventory/host_vars/eq12/vault.yml` currently has a commented-out
template showing which variables are needed. You'll replace it with a real encrypted
file:

```bash
cd ansible

# This opens your $EDITOR with a blank file. Paste your secrets in YAML format.
ansible-vault create inventory/host_vars/eq12/vault.yml
```

When your editor opens, paste the secrets (with real values):

```yaml
---
vault_postgres_password: "your-actual-postgres-password"
vault_pgadmin_email: "your-email@example.com"
vault_pgadmin_password: "your-pgadmin-password"
vault_grafana_user: "admin"
vault_grafana_password: "your-grafana-password"
vault_grafana_root_url: "http://grafana.yourdomain.com"
vault_grafana_domain: "yourdomain.com"
vault_vm_auth_username: "metrics"
vault_vm_auth_password: "your-vm-auth-password"
vault_vl_auth_username: "logs"
vault_vl_auth_password: "your-vl-auth-password"
vault_vaultwarden_domain: "https://vault.yourdomain.com"
vault_vaultwarden_admin_token: "your-admin-token"
vault_vaultwarden_signups_domains: "yourdomain.com"
vault_smtp_host: "smtp.gmail.com"
vault_smtp_from: "your-email@gmail.com"
vault_smtp_from_name: "Homelab"
vault_smtp_security: "starttls"
vault_smtp_port: "587"
vault_smtp_username: "your-email@gmail.com"
vault_smtp_password: "your-app-password"
vault_smtp_auth_mechanism: "Login"
vault_joplin_base_url: "http://joplin.yourdomain.com"
vault_joplin_postgres_password: "your-joplin-db-password"
vault_joplin_gmail_email: "your-email@gmail.com"
vault_joplin_gmail_app_password: "your-app-password"
vault_searxng_hostname: "searx.yourdomain.com"
vault_searxng_secret: "a-random-secret-string"
vault_watchtower_email_from: "your-email@gmail.com"
vault_watchtower_email_to: "your-email@gmail.com"
vault_watchtower_email_server: "smtp.gmail.com"
vault_watchtower_email_port: "587"
vault_watchtower_email_user: "your-email@gmail.com"
vault_watchtower_email_password: "your-app-password"
```

> **Where do these values come from?** They are the same passwords currently in the
> `.env` files on your deb-docker LXC. SSH in and check:
> `ssh root@deb-docker.lan 'cat /data/compose/*/docker-compose.yml'`
> or check the `.env` files if they exist alongside the compose files managed by Portainer.

Save and close. The file is now encrypted. You can verify:

```bash
# This shows garbage (encrypted). Good.
cat inventory/host_vars/eq12/vault.yml

# This shows the decrypted contents (needs .vault_password). Good.
ansible-vault view inventory/host_vars/eq12/vault.yml
```

### 3c. Create vaults for n5pro and shared secrets

```bash
# N5 Pro vault (currently just needs Proxmox API token)
ansible-vault create inventory/host_vars/n5pro/vault.yml

# Shared vault (secrets common to both machines)
ansible-vault create inventory/group_vars/all/vault.yml
```

### 3d. Edit a vault later

```bash
# Use the Taskfile shortcut:
task vault:edit -- inventory/host_vars/eq12/vault.yml

# Or directly:
ansible-vault edit inventory/host_vars/eq12/vault.yml
```

**What's safe here?** Vault files are committed to Git as encrypted blobs. Without
your `.vault_password` file, they're unreadable. The `.vault_password` file itself
is gitignored.

---

## Step 4 — Ansible: Dry-Run Host Configuration

This is your first "real" Ansible operation against live servers. **We use `--check --diff`
to make it a dry run** — it connects to your hosts, evaluates what would change, and
shows you a diff. It changes **nothing**.

```bash
# Dry-run the Proxmox host configuration
task infra:hosts:check

# Or manually with more verbosity:
cd ansible
ansible-playbook playbooks/proxmox-hosts.yml --check --diff -v
```

**Reading the output:**

```
TASK [common : Set timezone] ********************
ok: [eq12]          ← Already correct, no change needed

TASK [common : Install common packages] *********
changed: [eq12]     ← Would install htop, wget, etc. (but didn't — dry run)

TASK [proxmox_host : Enable Proxmox no-subscription repository] ***
changed: [eq12]     ← Would add the repo file (dry run)
```

- **`ok`** = Already in the desired state. No action needed.
- **`changed`** = Would make this change. In `--check` mode, it didn't actually do it.
- **`skipped`** = Condition not met (e.g., GPU passthrough tasks skip on EQ12).

Review the diff output carefully. You should see things like:

- "Would add packages: htop, wget, curl" → harmless
- "Would add apt repository" → harmless
- "Would modify /etc/default/grub" → only on N5 Pro for IOMMU, review the diff

**⚠️ What to watch for:** If you see any task that says it would *remove* or *stop*
something you're currently using, stop and investigate before proceeding.

---

## Step 5 — Migrate Stacks from Portainer to Ansible

Your Docker containers are currently managed via Portainer's "Stacks" feature. When
you deploy a stack through Portainer's UI, it stores its own versioned copy of the
compose file under `/data/compose/<stack_id>/v<version>/docker-compose.yml`. Portainer
tracks these stacks in its internal database.

Ansible's service roles deploy compose files to `/data/deploy/<service>/` and run
`docker compose up` from there. To avoid two managers fighting over the same containers,
we need to **remove Portainer's ownership** of each stack before Ansible takes over.

This does **not** stop, delete, or recreate your containers. It only removes Portainer's
internal record that it "owns" the stack. The containers keep running the entire time.

### 8a. Understand what's currently running

Your stacks as Portainer sees them (from Docker's perspective):

| Portainer Stack | Compose Source | Containers |
|----------------|----------------|------------|
| postgres | `/data/compose/11/v10/docker-compose.yml` | postgres, pgadmin4 |
| observability | `/data/compose/22/v30/docker-compose.yml` | grafana, victoriametrics, victorialogs, vector, telegraf |
| vaultwarden | `/data/compose/6/v4/docker-compose.yml` | vaultwarden |
| searxng | `/data/compose/19/v5/docker-compose.yml` | searxng |
| joplin | `/data/compose/18/v14/docker-compose.yml` | joplin-server |
| watchtower | `/data/compose/2/v4/docker-compose.yml` | watchtower |
| portainer | `portainer.yml` | portainer (self-managed) |

### 8b. Remove stacks from Portainer (one at a time)

Do this for each stack **except portainer itself** (portainer stays self-managed):

1. Open Portainer at `http://192.168.25.15:9000` (or via your browser)
2. Go to **Stacks** in the left sidebar
3. Click on the stack name (e.g., `postgres`)
4. Click the **Remove** button (red, top right area)
5. **CRITICAL**: In the confirmation dialog, **UNCHECK** "Remove associated resources
   (networks, volumes)" — this is the checkbox that would delete your data volumes
6. Confirm

**What happens:**
- Portainer removes its internal database record of the stack
- Portainer deletes its copy of the compose file from `/data/compose/<id>/`
- The actual Docker containers **keep running** — they are not stopped or removed
- Docker volumes (your databases, config, data) **are preserved**
- Docker networks **are preserved** (containers stay connected)

After removing, the containers will appear in Portainer under **Containers** as
standalone containers rather than part of a stack. You can still view logs, inspect
them, restart them — Portainer just won't try to manage their lifecycle via compose.

### 8c. Recommended order

Remove stacks in this order (least critical first, so you get comfortable with the
process before touching important services):

1. **watchtower** — just an auto-updater, no data
2. **searxng** — search engine, no persistent state that matters
3. **joplin** — data is in PostgreSQL, not a local volume
4. **vaultwarden** — data is in a Docker volume (safe since we don't remove volumes)
5. **observability** — 5 containers, largest stack
6. **postgres** — your database; leave for last since other services depend on it

**Do NOT remove `portainer` from Portainer.** Portainer manages itself — let it continue
to do so. Ansible's portainer role will just ensure the compose file and config stay
in sync.

### 8d. Verify containers are still running after each removal

After removing each stack from Portainer:

```bash
ssh root@deb-docker.lan 'docker ps --format "table {{.Names}}\t{{.Status}}"'
```

All containers should still show `Up`. If any stopped (they shouldn't), start them:

```bash
ssh root@deb-docker.lan 'docker start <container_name>'
```

### 8e. Clean up Portainer's old compose directories (optional)

After removing all stacks, Portainer's compose directories may have leftovers:

```bash
# See what's left
ssh root@deb-docker.lan 'ls -la /data/compose/'

# If empty or only has stale directories, clean up:
ssh root@deb-docker.lan 'rm -rf /data/compose/[0-9]*'
```

This is purely cleanup — those files are Portainer's old copies and are not used by
anything after stack removal.

### 8f. What about Portainer going forward?

Portainer remains installed and useful as a **monitoring dashboard**:
- View container logs in a web UI
- Inspect container details, resource usage, networking
- Start/stop/restart individual containers in emergencies
- Browse Docker volumes and networks

It just won't manage stack deployments anymore — Ansible handles that. Think of
Portainer as a read-mostly GUI on top of Docker, and Ansible as the actual deployment
tool.

---

## Step 6 — Ansible: Dry-Run Service Deployment

Now test whether Ansible's service deployment would disrupt your running containers.

```bash
# Dry-run deploying all services to deb-docker
task deploy:services -- --check --diff
```

**What to look for in the output:**

```
TASK [services/postgresql : Synchronize PostgreSQL compose and config files] ***
ok: [eq12_docker]    ← Files already match. No changes.

TASK [services/postgresql : Template PostgreSQL .env file] ***
changed: [eq12_docker]  ← Would create/update the .env file (dry run)

TASK [services/postgresql : Deploy PostgreSQL stack] ***
ok: [eq12_docker]    ← Docker stack already running with same config. No change.
```

The key safety features in our service roles:

1. **`synchronize` with `delete: false`**: rsync copies *new and changed* files but
   **never deletes** files that exist on the target but not in the source. Your data
   directories are safe.

2. **`rsync_opts: ["--exclude=data/", "--exclude=backups/"]`**: Data directories are
   explicitly excluded from sync. Even if rsync had `delete: true` (it doesn't),
   your database files would be untouched.

3. **`docker_compose_v2`**: This module runs `docker compose up -d` which:
   - Recreates containers only if their config (image, env, ports) changed
   - Leaves running containers alone if nothing changed
   - **Never removes volumes** — your PostgreSQL data, Grafana dashboards, etc. persist

4. **`.env` templating**: The `.env` file is templated from vault secrets. If the
   resulting file is identical to what's already on disk, Ansible reports `ok` (no change).
   If different, it overwrites the `.env` — but this only takes effect when Docker
   Compose re-reads it (on next `docker compose up`).

### Test a single service first

```bash
# Dry-run just PostgreSQL
task deploy:service -- --tags postgresql --check --diff
```

This limits the dry run to the `postgresql` role only. If you're satisfied, do one
service at a time for real:

```bash
# Deploy just PostgreSQL for real (no --check)
task deploy:service -- --tags postgresql
```

---

## Step 7 — Go Live

Once all dry runs look clean, execute the real operations **in this exact order**:

### 7a. Apply Ansible host configuration

```bash
# Configure Proxmox host OS (packages, repos, ZFS)
task infra:hosts
```

This installs packages and configures system settings. It does **not** restart
services or reboot. The only handler that triggers automatically is `update-grub`
on N5 Pro (for IOMMU), which updates the GRUB config but doesn't reboot.

### 7b. Apply Ansible guest configuration

```bash
# Configure Docker inside LXCs (install/update Docker, daemon.json)
task infra:guests
```

On deb-docker (CT 101), this ensures Docker and the Compose plugin are installed.
Since Docker is already installed and running, most tasks will report `ok`. If the
Docker daemon.json changes, Docker will restart (via handler), but **running containers
survive a Docker daemon restart** — they continue running.

### 7c. Deploy services one at a time

```bash
# Deploy each service individually, checking output as you go
task deploy:service -- --tags postgresql
task deploy:service -- --tags observability
task deploy:service -- --tags vaultwarden
task deploy:service -- --tags searxng
task deploy:service -- --tags joplin
task deploy:service -- --tags portainer
task deploy:service -- --tags watchtower
```

After each one, verify the service is still running:

```bash
ssh root@deb-docker.lan 'docker ps --format "table {{.Names}}\t{{.Status}}"'
```

Or all at once if you're confident:

```bash
task deploy:services
```

---

## Rollback & Recovery

### "Ansible changed a file I didn't want changed"

Ansible's `synchronize` module is rsync-based. By default our roles use `delete: false`,
so no files are removed. If a config file was overwritten:

```bash
# Ansible creates backups for lineinfile operations.
# Check /etc/ for .bak files:
ssh root@pve.lan 'find /etc -name "*.bak" -newer /etc/hostname'
```

### "A Docker container stopped after Ansible ran"

```bash
# Check what happened
ssh root@deb-docker.lan 'docker ps -a'

# Restart the container
ssh root@deb-docker.lan 'docker start <container_name>'

# Or re-run just that service's Ansible role
task deploy:service -- --tags postgresql
```

### "I lost my vault password"

If you lose `.vault_password`, you cannot decrypt your vault files. You'll need to:

1. Re-create `.vault_password` with a new password
2. Re-create all `vault.yml` files with `ansible-vault create`
3. Re-enter all secrets manually

**Prevention:** Store the vault password in Bitwarden/Vaultwarden.

## Glossary

| Term                  | Definition |
|-----------------------|------------|
| **IaC**               | Infrastructure as Code — defining infrastructure in version-controlled files instead of clicking through GUIs |
| **Idempotent**        | Running the same operation twice produces the same result. "Install htop" does nothing if htop is already installed. |
| **Inventory**         | Ansible's list of hosts, organized into groups. Lives in `ansible/inventory/`. |
| **Playbook**          | A YAML file that defines a sequence of automation tasks to run. |
| **Role**              | A reusable, organized collection of tasks, templates, and variables with a standard directory structure. |
| **Vault**             | Ansible's encryption system for secrets. Not to be confused with HashiCorp Vault or Vaultwarden. |
| **Vault password**    | The single password that encrypts/decrypts all vault files. Stored in `.vault_password` (gitignored). |
| **Check mode**        | Ansible's `--check` flag. Simulates a run without making changes. |
| **Handler**           | An Ansible task that runs only when triggered by a change (e.g., "restart Docker" only if `daemon.json` changed). |
| **Tag**               | A label on Ansible tasks/roles that lets you selectively run subsets (e.g., `--tags postgresql`). |
| **Synchronize**       | An Ansible module that wraps rsync. Copies files from your Mac to the remote host efficiently. |
| **docker_compose_v2** | An Ansible module that runs `docker compose up/down/pull`. Used by our service roles to deploy stacks. |

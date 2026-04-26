# Onboarding Guide

How to bring your existing, running homelab infrastructure under Pulumi and Ansible
management **without losing data or disrupting running services**.

> **The golden rule**: Every step in this guide is read-only or additive by default.
> Nothing deletes containers, VMs, volumes, or databases. If a command says "destroy"
> or "recreate", it is explicitly called out with a ⚠️ warning.

---

## Table of Contents

1. [Concepts: What Pulumi and Ansible Do](#1-concepts-what-pulumi-and-ansible-do)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Ansible: Install Tools on Your Mac](#step-1--ansible-install-tools-on-your-mac)
4. [Step 2 — Ansible: Test Connectivity](#step-2--ansible-test-connectivity)
5. [Step 3 — Ansible: Encrypt Your Secrets](#step-3--ansible-encrypt-your-secrets)
6. [Step 4 — Ansible: Dry-Run Host Configuration](#step-4--ansible-dry-run-host-configuration)
7. [Step 5 — Pulumi: Install and Initialize](#step-5--pulumi-install-and-initialize)
8. [Step 6 — Pulumi: Import Existing Resources](#step-6--pulumi-import-existing-resources)
9. [Step 7 — Pulumi: Verify State Matches Reality](#step-7--pulumi-verify-state-matches-reality)
10. [Step 8 — Migrate Stacks from Portainer to Ansible](#step-8--migrate-stacks-from-portainer-to-ansible)
11. [Step 9 — Ansible: Dry-Run Service Deployment](#step-9--ansible-dry-run-service-deployment)
12. [Step 10 — Go Live](#step-10--go-live)
13. [Rollback & Recovery](#rollback--recovery)
14. [Glossary](#glossary)

---

## 1. Concepts: What Pulumi and Ansible Do

### Pulumi (Infrastructure Lifecycle)

Pulumi is an **Infrastructure as Code (IaC)** tool. You describe the desired state of
your VMs and LXC containers in TypeScript, and Pulumi figures out what API calls to
make to Proxmox to make reality match your code.

**Key concepts:**

| Term             | What it means |
|------------------|---------------|
| **Stack**        | A named deployment environment (we use `prod`). Think of it as a saved snapshot of "what Pulumi thinks exists." |
| **State**        | A JSON file that records every resource Pulumi manages — IDs, properties, dependencies. Stored locally in `~/.pulumi` by default. |
| **Preview**      | Shows what Pulumi *would* do without actually doing it. Like a diff. Always safe. |
| **Up**           | Applies the changes shown in preview. Creates, updates, or deletes resources. |
| **Import**       | Tells Pulumi "this resource already exists, don't create it — just start tracking it." **This is the critical command for onboarding.** |
| **Refresh**      | Re-reads the actual state from Proxmox and updates Pulumi's state file to match. Doesn't change any real infrastructure. |
| **Resource URN** | A unique identifier for each resource in Pulumi's world, like `urn:pulumi:prod::homelab-infrastructure::homelab:proxmox:LxcContainer::eq12-deb-docker`. |

**Why we need `import`:** Your VMs/LXCs already exist on Proxmox. If you just run
`pulumi up`, Pulumi would try to *create* them because it has no record of them.
The `import` command says "Proxmox already has VM 100 — start tracking it, don't
create a duplicate."

### Ansible (Configuration Management)

Ansible is a **configuration management** and **automation** tool. You describe what
packages should be installed, what files should exist, and what services should be
running — Ansible makes it so via SSH.

**Key concepts:**

| Term                       | What it means |
|----------------------------|---------------|
| **Inventory**              | The list of machines Ansible manages (`ansible/inventory/hosts.yml`). Groups machines by role (e.g., `proxmox_hosts`, `docker_hosts`). |
| **Playbook**               | A YAML file describing a sequence of tasks to run on a set of hosts. Like a recipe. |
| **Role**                   | A reusable bundle of tasks, templates, and defaults. Our roles live in `ansible/roles/`. |
| **Task**                   | A single action: install a package, copy a file, start a service, deploy a Docker stack. |
| **Handler**                | A task that only runs when *notified* — e.g., restart Docker only if its config changed. |
| **Idempotent**             | Running the same playbook twice produces the same result. If a package is already installed, Ansible skips it. If a file already has the right content, Ansible leaves it. This is what makes Ansible safe to re-run. |
| **Check mode** (`--check`) | Dry-run mode. Ansible shows what it *would* change without actually changing anything. Always safe. |
| **Diff** (`--diff`)        | Shows the exact line-by-line differences Ansible would apply. Combine with `--check` for a safe preview. |
| **Vault**                  | Ansible's built-in encryption for secrets. It encrypts YAML files so passwords never appear in plain text in your Git repository. |
| **Tags**                   | Labels on tasks that let you run only a subset. E.g., `--tags postgresql` runs only PostgreSQL deployment. |

**What makes Ansible safe for onboarding:** Most Ansible modules are idempotent. If
Docker is already installed, Ansible won't reinstall it. If a compose file already
exists, `synchronize` (rsync) only copies changed files. The `docker_compose_v2`
module brings a stack to the declared state — if it's already running with the same
config, it does nothing.

### How They Work Together

```ascii
┌─────────────────────────────────────────────────────┐
│  1. Ansible: Configure Proxmox hosts                │
│     (packages, networking, ZFS, GPU passthrough)    │
│                                                     │
│  2. Pulumi: Create/manage VM and LXC lifecycles     │
│     (create, resize, destroy VMs/CTs)               │
│                                                     │
│  3. Ansible: Configure guests (Docker, packages)    │
│     (inside the VMs/LXCs Pulumi created)            │
│                                                     │
│  4. Ansible: Deploy Docker Compose services         │
│     (syncs compose files, templates .env, runs up)  │
└─────────────────────────────────────────────────────┘
```

---

## 2. Prerequisites

Install these on your Mac before starting:

```bash
# Homebrew packages
brew install ansible pulumi node go-task

# Verify versions
ansible --version    # 2.20.4
pulumi version       # v3.230.0
node --version       # v25.9.0 (for Pulumi TypeScript)
task --version       # 3.49.1
```

You also need:

- **SSH access**: You should already be able to `ssh root@pve.lan` and `ssh root@deb-docker.lan`
- **A Proxmox API token** (created later in the Pulumi section)

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

## Step 5 — Pulumi: Install and Initialize

### 5a. Install dependencies

```bash
cd infrastructure
npm install
```

This downloads the Pulumi SDK and the Proxmox provider to `node_modules/`. Nothing
touches your servers — this is all local.

### 5b. Set up local state backend

By default, Pulumi wants to store state in their cloud service. For a homelab,
local file storage is simpler and has no external dependencies:

```bash
# Tell Pulumi to store state on your local filesystem
pulumi login --local
```

This stores state in `~/.pulumi/` on your Mac. **Back this directory up** — if you
lose it, Pulumi won't know what it manages and you'd need to re-import everything.

### 5c. Initialize the stack

```bash
# Still in infrastructure/
pulumi stack init prod
```

This creates an empty state file for a stack named `prod`. Pulumi now exists but
knows about zero resources.

### 5d. Create a Proxmox API token

Pulumi needs to talk to the Proxmox API. On EQ12:

```bash
ssh root@pve.lan

# Create an API token (on Proxmox)
pveum user token add root@pam pulumi --privsep=0
```

This prints a token like: `root@pam!pulumi=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

Copy it. Then back on your Mac:

```bash
cd infrastructure

# Store the token as an encrypted secret in Pulumi config
pulumi config set --secret proxmoxve:apiToken "root@pam!pulumi=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# Set the endpoint
pulumi config set homelab-infrastructure:eq12Endpoint "https://192.168.25.5:8006"
```

Repeat for N5 Pro if you want Pulumi to manage that host too.

**What's safe here?** The API token is encrypted in `Pulumi.prod.yaml` using a
passphrase. The secret never appears in plain text in Git. Pulumi will ask for
the passphrase when you run commands (or you can set `PULUMI_CONFIG_PASSPHRASE`).

---

## Step 6 — Pulumi: Import Existing Resources

This is the most important step. **Import tells Pulumi: "This resource already exists
on Proxmox. Don't create it — just start tracking it in your state."**

### How import works

```
Before import:
  Pulumi state: (empty)
  Proxmox: VM 100 exists, CT 101 exists, CT 102 exists, CT 104 exists
  
  If you ran "pulumi up" now → Pulumi would try to CREATE all four ← BAD!

After import:
  Pulumi state: VM 100 ✓, CT 101 ✓, CT 102 ✓, CT 104 ✓
  Proxmox: VM 100 exists, CT 101 exists, CT 102 exists, CT 104 exists
  
  If you ran "pulumi up" now → Pulumi sees they match → does nothing ← GOOD!
```

### 6a. Import EQ12 resources

Run these commands from the `infrastructure/` directory. Each import:

1. Reads the actual resource from Proxmox via its API
2. Records it in Pulumi's local state file
3. Does **not** modify the resource in any way

```bash
cd infrastructure

# Import VM 100 (Home Assistant)
# Syntax: pulumi import <type> <pulumi-name> <proxmox-id>
# The type comes from our ComponentResource in the code
# The proxmox-id for VMs/CTs is typically "node/vmid" format

pulumi import "proxmoxve:VM/virtualMachine:VirtualMachine" \
  "eq12-homeassistant" \
  "pve/qemu/100" \
  --parent "urn:pulumi:prod::homelab-infrastructure::homelab:proxmox:VirtualMachine::eq12-homeassistant"

# Import CT 101 (deb-docker)
pulumi import "proxmoxve:CT/container:Container" \
  "eq12-deb-docker" \
  "pve/lxc/101" \
  --parent "urn:pulumi:prod::homelab-infrastructure::homelab:proxmox:LxcContainer::eq12-deb-docker"

# Import CT 102 (ubuntu-docker)
pulumi import "proxmoxve:CT/container:Container" \
  "eq12-ubuntu-docker" \
  "pve/lxc/102" \
  --parent "urn:pulumi:prod::homelab-infrastructure::homelab:proxmox:LxcContainer::eq12-ubuntu-docker"

# Import CT 104 (nginxproxymanager)
pulumi import "proxmoxve:CT/container:Container" \
  "eq12-npm" \
  "pve/lxc/104" \
  --parent "urn:pulumi:prod::homelab-infrastructure::homelab:proxmox:LxcContainer::eq12-npm"
```

> **Note on IDs**: The exact resource ID format (`pve/qemu/100` vs `100` vs
> `node/pve/qemu/100`) depends on the provider version. If an import fails with
> "resource not found", try variations:
>
> - `100`
> - `pve/100`
> - `pve/qemu/100`
> - `node/pve/qemu/100`
>
> Check the provider docs: <https://github.com/muhlba91/pulumi-proxmoxve>

After each import, Pulumi will show you the imported resource's properties and may
print a code snippet. **You don't need to paste that code** — the definitions already
exist in `machines/eq12.ts`.

### 6b. Handle import diffs

After importing, Pulumi compares the imported state to your code. If there are
differences (e.g., your code says 6GB root but the real container has 8GB), Pulumi
will show them in the next `pulumi preview`.

**This is expected!** You may need to adjust the code in `machines/eq12.ts` to exactly
match the real configuration. We already tried to match reality when writing the code,
but there may be subtle differences (different default values, properties you didn't
set explicitly, etc.).

The goal: run `pulumi preview` and see **no changes**.

---

## Step 7 — Pulumi: Verify State Matches Reality

```bash
cd infrastructure

# Preview what Pulumi thinks needs to change
pulumi preview --diff
```

**Reading the output:**

```
Previewing update (prod):

     Type                              Name              Plan
     pulumi:pulumi:Stack               homelab-prod      
     ├─ homelab:proxmox:VirtualMachine eq12-homeassistant
     ├─ homelab:proxmox:LxcContainer   eq12-deb-docker   
     ...

Resources:
    4 unchanged
```

- **"4 unchanged"** → Perfect. Pulumi's state matches reality and your code.
- **"~ update"** → Pulumi wants to change something. Read the diff carefully.

### If you see updates

Example: Pulumi wants to change the memory of CT 101 from 4096 to 2048.

This means your code says `memory: 2048` but the real container has 4096 MB.
Fix the code to match reality:

```typescript
// machines/eq12.ts — change to match the actual value
memory: 4096, // 4GB
```

Then run `pulumi preview --diff` again. Repeat until you see zero changes.

### If you see creates

Pulumi wants to create a resource that already exists. This means the import
didn't work for that resource. Re-run the import command for it.

### If you see deletes

**⚠️ STOP.** Pulumi wants to delete something. This could mean:

- You removed a resource definition from your code → add it back
- The import mapped to the wrong resource → re-import with the correct ID
- There's a naming mismatch → check the Pulumi name matches your code

**Never run `pulumi up` if the preview shows unexpected deletes.**

---

## Step 8 — Migrate Stacks from Portainer to Ansible

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

## Step 9 — Ansible: Dry-Run Service Deployment

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

## Step 10 — Go Live

Once all dry runs look clean, execute the real operations **in this exact order**:

### 10a. Apply Ansible host configuration

```bash
# Configure Proxmox host OS (packages, repos, ZFS)
task infra:hosts
```

This installs packages and configures system settings. It does **not** restart
services or reboot. The only handler that triggers automatically is `update-grub`
on N5 Pro (for IOMMU), which updates the GRUB config but doesn't reboot.

### 10b. Verify Pulumi state (no apply needed yet)

```bash
cd infrastructure
pulumi preview --diff
```

If zero changes → you're good. The import from Step 6 already adopted your resources.
You don't need to `pulumi up` unless the preview shows updates you want to apply.

### 10c. Apply Ansible guest configuration

```bash
# Configure Docker inside LXCs (install/update Docker, daemon.json)
task infra:guests
```

On deb-docker (CT 101), this ensures Docker and the Compose plugin are installed.
Since Docker is already installed and running, most tasks will report `ok`. If the
Docker daemon.json changes, Docker will restart (via handler), but **running containers
survive a Docker daemon restart** — they continue running.

### 10d. Deploy services one at a time

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

### "Pulumi did something unexpected"

```bash
# See what Pulumi changed
pulumi stack export | jq '.deployment.resources[] | .urn'

# Refresh state from Proxmox (reads real state, changes nothing on Proxmox)
pulumi refresh

# If a resource was modified and you want to revert:
# Fix the code → pulumi up (to apply the fix)
```

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

### Nuclear option: start Pulumi state from scratch

If Pulumi state gets corrupted or out of sync, you can wipe it and re-import.
This does **not** affect your actual VMs/LXCs — only Pulumi's local records.

```bash
cd infrastructure
pulumi stack rm prod --force   # Delete the state
pulumi stack init prod         # Create fresh state
# Then re-run all import commands from Step 6
```

---

## Glossary

| Term                  | Definition |
|-----------------------|------------|
| **IaC**               | Infrastructure as Code — defining infrastructure in version-controlled files instead of clicking through GUIs |
| **Idempotent**        | Running the same operation twice produces the same result. "Install htop" does nothing if htop is already installed. |
| **State file**        | Pulumi's record of what it manages. A JSON file stored in `~/.pulumi/`. |
| **Inventory**         | Ansible's list of hosts, organized into groups. Lives in `ansible/inventory/`. |
| **Playbook**          | A YAML file that defines a sequence of automation tasks to run. |
| **Role**              | A reusable, organized collection of tasks, templates, and variables with a standard directory structure. |
| **Vault**             | Ansible's encryption system for secrets. Not to be confused with HashiCorp Vault or Vaultwarden. |
| **Vault password**    | The single password that encrypts/decrypts all vault files. Stored in `.vault_password` (gitignored). |
| **Stack**             | A Pulumi deployment target (like "prod" or "staging"). Each stack has its own independent state. |
| **Provider**          | A Pulumi plugin that knows how to talk to a specific API (in our case, `@muhlba91/pulumi-proxmoxve` talks to Proxmox). |
| **ComponentResource** | A Pulumi class that bundles related resources together. Our `LxcContainer` and `VirtualMachine` classes wrap the raw provider resources with sensible defaults. |
| **Check mode**        | Ansible's `--check` flag. Simulates a run without making changes. |
| **Handler**           | An Ansible task that runs only when triggered by a change (e.g., "restart Docker" only if `daemon.json` changed). |
| **Tag**               | A label on Ansible tasks/roles that lets you selectively run subsets (e.g., `--tags postgresql`). |
| **Synchronize**       | An Ansible module that wraps rsync. Copies files from your Mac to the remote host efficiently. |
| **docker_compose_v2** | An Ansible module that runs `docker compose up/down/pull`. Used by our service roles to deploy stacks. |
| **URN**               | Uniform Resource Name — Pulumi's unique identifier for each managed resource. |

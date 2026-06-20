---
title: "Docker Compose services fail to start in privileged LXC due to AppArmor docker-default profile"
date: 2026-06-20
category: integration-issues
module: docker_host
problem_type: integration_issue
component: tooling
symptoms:
  - "Error response from daemon AppArmor enabled on system but the docker-default profile could not be loaded"
  - "apparmor_parser Access denied. You need policy admin privileges to manage profiles"
  - "docker compose up aborts at container creation for every service"
root_cause: missing_permission
resolution_type: config_change
severity: high
related_components:
  - proxmox
  - docker-compose
  - lxc
tags:
  - docker
  - apparmor
  - lxc
  - proxmox
  - ansible
  - privileged-container
---

# Docker Compose services fail to start in privileged LXC due to AppArmor docker-default profile

## Problem

Docker Compose services failed to start inside the privileged LXC Docker hosts
(`n5pro-docker` CT 201, `eq12-docker` CT 101) on Proxmox. Docker detects AppArmor
in the host kernel and tries to load its `docker-default` profile, but a
privileged LXC container lacks the policy-admin privileges to manage AppArmor
profiles. No containerized service could be deployed until both fix layers were
applied.

## Symptoms

- "Error response from daemon: AppArmor enabled on system but the docker-default profile could not be loaded: running '/usr/sbin/apparmor_parser -Kr' failed"
- "apparmor_parser: Unable to replace \"docker-default\". apparmor_parser: Access denied. You need policy admin privileges to manage profiles."
- `docker compose up` aborts immediately at container creation; no container reaches running state.
- Reproducible on every service across all stacks (Portainer hit first, then Watchtower, etc.), only inside the privileged LXC hosts.

## What Didn't Work

- "Masking AppArmor inside the LXC alone: stopping/masking the apparmor service did not stop Docker from detecting AppArmor at the kernel level and attempting to load the profile per-container."
- "Reordering left incomplete: the mask tasks existed in the docker_host role but ran AFTER 'Configure Docker daemon' and 'Ensure Docker is started', so Docker started while AppArmor was still active."
- "Editing /etc/docker/daemon.json over SSH: this ad-hoc change was reverted because all homelab changes must go through Ansible; daemon.json.j2 was deliberately left without any AppArmor directive."

## Solution

Two layers were required; neither alone was sufficient.

**Layer 1 — Reorder the AppArmor stop/mask block to run *before* Docker config and
start** (`ansible/roles/docker_host/tasks/main.yml`). New order:

1. Install Docker Engine
2. Stop and mask AppArmor (`when: docker_lxc | default(false)`) ← moved up
3. Configure Docker daemon
4. Ensure Docker is started

```yaml
- name: Stop and disable AppArmor (Docker-in-LXC workaround)
  when: docker_lxc | default(false)
  block:
    - name: Stop AppArmor service
      ansible.builtin.systemd:
        name: apparmor
        state: stopped
        enabled: false
      failed_when: false
    - name: Mask AppArmor to prevent re-activation
      ansible.builtin.systemd:
        name: apparmor
        masked: true

- name: Configure Docker daemon
  ansible.builtin.template:
    src: daemon.json.j2
    dest: /etc/docker/daemon.json
    # ...
- name: Ensure Docker is started and enabled
  # ...
```

**Layer 2 — Add `security_opt: ["apparmor:unconfined"]` to every service** in all 11
compose files under `containers/`.

```yaml
services:
  portainer:
    image: portainer/portainer-ee:lts
    container_name: portainer
    restart: always
    security_opt:
      - "apparmor:unconfined"
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
```

Coverage — 19 service definitions across 11 files: portainer (1), watchtower (1),
joplin (1), immich (3), frigate (1), nextcloud (2), lyrion (1), vaultwarden (1),
postgresql (2), observability (5), searxng (1). Committed in `1049e7b`.

## Why This Works

AppArmor enforcement is a kernel-level feature. Docker probes the kernel and, if
it sees AppArmor available, tries to install and apply its `docker-default`
profile via `apparmor_parser -Kr` for each container. A privileged LXC shares the
host kernel but does not hold the policy-admin privileges that profile management
requires, so `apparmor_parser` returns "Access denied" — profile management is a
host-only operation.

- Layer 1 (mask before Docker starts) ensures the in-container apparmor service is
  not running and Docker comes up clean — but masking alone does not change what
  Docker detects from the kernel.
- Layer 2 (`apparmor:unconfined` per service) explicitly tells Docker not to apply
  any profile to that container, bypassing the `apparmor_parser` call entirely.
  This is the directive that actually prevents the failing load attempt.

Editing `daemon.json` was unnecessary and was kept out of the template.

## Prevention

- **Every new compose service** added under `containers/` must include
  `security_opt: ["apparmor:unconfined"]` — both Docker hosts are privileged LXCs.
  Apply it to *each* service in a multi-service file, not just the first.
- **Keep the AppArmor stop/mask block ordered before** "Configure Docker daemon"
  and "Ensure Docker is started" in `docker_host/tasks/main.yml`; do not move it back.
- **Do not** add AppArmor workarounds to `daemon.json.j2` or apply them over SSH —
  all changes go through Ansible and the daemon template is intentionally minimal.

## Related Issues

- [TrueNAS ISO get_url existence guard](../runtime-errors/ansible-get-url-truenas-iso-existence-guard.md) — surfaced in the same `deploy:full` run.

---
title: "Parameterizing a shared Docker Compose network subnet forces a destructive recreate"
date: 2026-06-20
category: integration-issues
module: containers
problem_type: integration_issue
component: tooling
symptoms:
  - "error while removing network: network <name> has active endpoints"
  - "docker compose up stops the stack's containers then fails to recreate the network"
  - "postgres/pgadmin left Exited after a deploy that only changed a subnet definition"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - docker-compose
  - postgresql
  - ansible
tags:
  - docker
  - docker-compose
  - networks
  - shared-network
  - external-network
  - idempotency
---

# Parameterizing a shared Docker Compose network subnet forces a destructive recreate

## Problem

Changing a Compose network's `ipam` subnet **definition** — even to a templated
value with the *same* result (`subnet: 172.21.0.0/24` → `subnet: ${X:-172.21.0.0/24}`) —
changes the network's compose config hash. On the next `docker compose up`,
Compose tries to **recreate** the network. If that network is **shared** (one
stack defines it; others attach via `external: true`), the removal fails because
the other stacks still hold endpoints, and the owning stack's containers are left
stopped.

## Symptoms

- "Error response from daemon: error while removing network: network postgres_network has active endpoints (name:\"joplin-server\" ...)"
- The deploy stops `postgres` and `pgadmin`, removes the network, then errors — leaving the DB down.
- Triggered by a change that looked value-neutral (literal subnet → `${VAR:-samevalue}`).

## What Didn't Work

- Assuming a same-value parameterization is a no-op. Compose compares the config
  hash, not the resolved value, so it still recreates.
- A `--check` dry-run did not surface it — check mode simulates the compose step;
  the recreate only happens on a real `up`.

## Solution

Keep **shared/external** network definitions literal. Only parameterize networks
that are **private** to a single stack (where a recreate just bounces that one
stack's own containers, which is acceptable).

```yaml
# postgresql.yml — postgres_network is shared (joplin/immich/nextcloud attach
# external). Keep literal; do NOT use ${VAR}.
networks:
  postgres_network:
    ipam:
      config:
        - subnet: 172.21.0.0/24
          gateway: 172.21.0.1
```

Recovery when it has already happened: revert the shared network to its literal
definition and re-run the owning stack's role via Ansible (not ad-hoc shell) —
with the definition stable again, `up` re-attaches without a recreate.

## Why This Works

Compose only recreates a network when its definition hash changes. A literal,
unchanging definition keeps the hash stable, so a shared network is never torn
down out from under the stacks attached to it. Private networks can change freely
because nothing else depends on them.

## Prevention

- Before parameterizing a Compose network subnet, check whether the network is
  referenced as `external: true` by any other stack
  (`grep -rl <network_name> containers/*/*.yml`). More than one reference = shared
  = leave literal.
- Deploy service/network changes to a live host and verify container state
  **before merge** — this class of failure is invisible to `--check` and to lint.
- See also [[ansible-change-loop-pitfalls]] (idempotency gates / verify-live).

## Related Issues

- Surfaced while resolving #11 (wire docker_networks var through services).
- [[docker-apparmor-privileged-lxc]] — other Docker-in-this-repo gotchas.

---
title: "docker compose up recreates any container whose last create was Watchtower's"
date: 2026-08-15
category: integration-issues
module: containers
problem_type: integration_issue
component: tooling
symptoms:
  - "a container is recreated by a deploy that changed nothing about it"
  - "compose recreates a container whose compose file, image and .env are all identical"
  - "the first Ansible deploy after a Watchtower update bounces that service"
root_cause: external_dependency
resolution_type: workaround
severity: medium
related_components:
  - docker-compose
  - watchtower
  - ansible
  - services/_deploy
tags:
  - docker
  - docker-compose
  - watchtower
  - recreate
  - idempotency
  - deploy
---

# docker compose up recreates any container whose last create was Watchtower's

## Problem

A container that Watchtower updated will be **recreated by the next `docker compose
up`**, even when the compose file is byte-identical to what is already deployed, the
image is the same, and the rendered `.env` is unchanged.

The practical consequence: after any Watchtower update, the next Ansible deploy bounces
that service. The bounce is triggered by the deploy, but it is not *caused* by whatever
the deploy changed — which makes it very easy to misattribute.

## Symptoms

- `docker compose up` recreates a container while a deploy that touched nothing about
  that service is running.
- The recreate survives every attempt to explain it from the diff: same image digest,
  same compose content, same environment file.
- Services never touched by Watchtower, deployed in the same run, are left alone.

## What Didn't Work

**`docker compose up --dry-run` as a recreate predictor.** It is useless for this. Run
as a control against a container that was provably stable across an actual deploy,
`--dry-run` still reported `Recreate`. It answers "what might compose touch", not "what
will change" — so a `--dry-run` that predicts a recreate is not evidence a recreate will
happen, and cannot be used to clear a change as recreate-free.

**Reasoning from the diff alone.** Because the compose payload was byte-identical, every
explanation derived from repo content predicted no recreate. The signal was not in the
repo at all; it was in the container's own creation lineage.

## Solution

Treat **container lineage** — which tool created the container that is running right now
— as a first-class input when predicting a deploy's blast radius.

Before an apply that must be recreate-free, partition the running containers:

- Containers whose most recent create was `docker compose` → expect them to survive.
- Containers whose most recent create was Watchtower → expect a recreate, regardless of
  the diff, and schedule accordingly.

Then decompose any recreate you observe against that partition first, before assuming
your change caused it.

The evidence from the #85 apply, stated in the strongest form it actually supports:
**no compose-created container was ever recreated by a byte-identical `up` (9/9), and
every *proven* Watchtower-created container was (4/4).**

The proven set is eq12's four recreated containers — postgres, pgadmin4, searxng and
portainer — each carrying a `Created` timestamp matching Watchtower's 04:30-local
schedule (11:30Z). All nine compose-created containers across both hosts survived.

Two further recreates on n5pro are *consistent* with the rule but are not evidence for
it, and are excluded from the count deliberately:

- **portainer (n5pro)** had an independent, proven cause: its deployed compose file was a
  hand-era revision that had drifted from the repo, so the first `up` after the move
  converged real content. Lineage is not needed to explain it.
- **lms (n5pro)** has unrecoverable lineage evidence — Watchtower's log history was wiped
  by its own redeploy, and the previous container was destroyed with the recreate. It
  cannot be attributed either way.

Two of the four proven cases also had an independent reason to recreate (an upstream
image published since the last deploy; a role that deliberately sets `recreate: always`
on a config change). That is why the rule is stated as lineage *predicting* a recreate
rather than being its only possible cause.

## Why This Works

Compose decides whether to reuse a container by comparing the container's recorded
configuration against the configuration it computes from the project — it does not
diff the compose file against the file it deployed last time. A container recreated
outside that flow is not carrying the bookkeeping a compose-created container carries,
so on the next `up` compose cannot match it to the project's expected state and
replaces it.

That is the mechanism as best it can be inferred from the observed behaviour, not a
verified reading of Compose's internals.

Be precise about how strong the evidence is. The partition was never falsified in this
run — nothing survived that the rule said would recreate, and nothing recreated that the
rule said would survive. But it was not confirmed across the board either: two recreates
are unattributable (one had an independent proven cause, one lost its lineage evidence),
and several of the proven cases had a second sufficient cause of their own. Treat the
rule as a reliable *predictor of risk*, not as a demonstrated law of Compose.

## Prevention

- **Do not treat "the payload is byte-identical" as proof that nothing will restart.**
  Byte-identity constrains what *your change* can do; it says nothing about
  reconciliation of state some other tool created.
- **Check lineage before promising a recreate-free deploy.** Inspect what created each
  running container rather than inferring it from the repo.
- **Never clear a change as recreate-free using `docker compose up --dry-run`** — see
  above; it over-predicts and a control test proves it.
- **Expect the first deploy after a Watchtower window to bounce the updated services**,
  and prefer to deploy outside that window when a bounce is costly.
- When a deploy produces an unexpected recreate, decompose before concluding: check
  lineage, check whether `pull: always` picked up a newly published upstream image, and
  check whether the role deliberately requests a recreate on a config change. Only then
  suspect the change under review.

Related: #83 (Watchtower update policy — which services are in scope for unattended
updates is exactly what determines which containers acquire Watchtower lineage). See
also `docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md` for how a
container ends up in or out of Watchtower's scan scope in the first place, and
`docs/solutions/conventions/collocating-compose-stacks-into-ansible-roles.md` for the
relocation whose apply surfaced this.

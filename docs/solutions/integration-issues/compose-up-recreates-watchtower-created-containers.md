---
title: "docker compose up recreates any container whose last create was Watchtower's"
date: 2026-08-15
last_updated: 2026-08-15
category: integration-issues
module: containers
problem_type: integration_issue
component: tooling
symptoms:
  - "a container is recreated by a deploy that changed nothing about it"
  - "compose recreates a container whose compose file, image and .env are all identical"
  - "the first Ansible deploy after a Watchtower update bounces that service"
  - "the Ansible compose module reports changed=1 on the run right after a manual docker compose up"
  - "a container is recreated even though its image digest is provably identical before and after"
root_cause: external_dependency
resolution_type: workaround
severity: medium
related_components:
  - docker-compose
  - watchtower
  - ansible
  - services/_deploy
  - community.docker.docker_compose_v2
  - services/observability
tags:
  - docker
  - docker-compose
  - watchtower
  - recreate
  - idempotency
  - deploy
  - ansible
  - container-lineage
---

# docker compose up recreates any container whose last create was Watchtower's

## Problem

A container that Watchtower updated will be **recreated by the next `docker compose
up`**, even when the compose file is byte-identical to what is already deployed, the
image is the same, and the rendered `.env` is unchanged.

The practical consequence: after any Watchtower update, the next Ansible deploy bounces
that service. The bounce is triggered by the deploy, but it is not *caused* by whatever
the deploy changed — which makes it very easy to misattribute.

Watchtower is the common case but not the only one. A manual `docker compose up` run
between two Ansible deploys produces the same effect for exactly one cycle — see
[Refinement: "compose lineage" is not a single lineage](#refinement-compose-lineage-is-not-a-single-lineage).

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

- Containers whose most recent create was `docker compose` *through the same front-end
  that is about to run* → expect them to survive.
- Containers whose most recent create was Watchtower → expect a recreate, regardless of
  the diff, and schedule accordingly.

Then decompose any recreate you observe against that partition first, before assuming
your change caused it.

The evidence from the #85 apply, stated in the strongest form it actually supports:
**no compose-created container was ever recreated by a byte-identical `up` (9/9), and
every *proven* Watchtower-created container was (4/4).**

Note the conditions that tally was measured under: every `up` in it was issued by the
same front-end (Ansible's `community.docker.docker_compose_v2`), with no manual CLI
`compose` command interleaved. The count stands for those conditions. What the #82 apply
later narrowed is the *generalization* drawn from it — see the refinement section below.

The proven set is eq12's four recreated containers — postgres, pgadmin4, searxng and
portainer — each carrying a `Created` timestamp matching Watchtower's 04:30-local
schedule (11:30Z). All nine compose-created containers across both hosts survived.

The #82 apply (Vector disk-buffer persistence, `eq12_docker`, 2026-08-15) added a second,
independent set of supporting cases — and corrected an attribution error worth recording,
because the error is the generalizable part.

eq12's grafana, telegraf, victoriametrics and victorialogs all carry
`Created 2026-08-14T06:02Z`. The pre-apply prediction called them Watchtower lineage and
therefore expected them to bounce. That was **wrong**. 06:02Z is 23:02 the previous day in
the host's local time — the vector-incident-night *compose* deploys. Watchtower's window on
this host is 04:30 local, i.e. 11:30Z, nowhere near it. All four are compose lineage.

Re-read against the correct lineage, they are supporting evidence rather than a lucky
near-miss:

- grafana, telegraf and victoriametrics were never reported as recreated in any of the
  three #82 applies.
- victorialogs was untouched by the apply of the change under review, and moved only on the
  run after a CLI `up` had been interleaved — the reconciliation described below, not a
  counter-example to the survival rule.

The method point generalizes past this incident: **lineage attribution by timestamp requires
converting the container's UTC `Created` value to the host's local time before comparing it
against Watchtower's local-time schedule.** A UTC-versus-local slip is exactly how four
compose-created containers got labelled as Watchtower's, and a predicted-bounce list that is
wrong in that direction quietly inflates the expected blast radius of every deploy planned
from it.

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
outside that flow — or created through a different compose front-end — is not carrying
exactly the bookkeeping the next `up` expects, so compose cannot match it to the
project's expected state and replaces it.

That is the mechanism as best it can be inferred from the observed behaviour, not a
verified reading of Compose's internals.

Be precise about how strong the evidence is. The partition was never falsified in this
run — nothing survived that the rule said would recreate, and nothing recreated that the
rule said would survive. But it was not confirmed across the board either: two recreates
are unattributable (one had an independent proven cause, one lost its lineage evidence),
and several of the proven cases had a second sufficient cause of their own. Treat the
rule as a reliable *predictor of risk*, not as a demonstrated law of Compose.

## Refinement: "compose lineage" is not a single lineage

The partition above splits containers by *whether* compose created them. The #82 apply
showed the split has to go one level finer: **by which compose front-end last acted on the
project.** A manual `docker compose` CLI command run between two Ansible deploys can cost
a recreate on the next module `up`, even though both front-ends are "compose" by the
coarse rule.

### The observed sequence

Three Ansible applies of the same branch against `eq12_docker`, with one deliberate CLI
step interleaved between the first and the second:

1. **Apply run 1** — `ok=19 changed=3 failed=0`. Exactly one container was recreated:
   vector (`8e3ca026` → `9bf2af7d`). The other 11 containers on the host kept their IDs.
   This is the change under review doing precisely what it was predicted to do.
2. **CLI step (buffer-survival proof)** — a deliberate
   `docker compose up -d --force-recreate vector`, run to prove the persistence fix:
   8 buffer/checkpoint files before, 8 after, `comm -23 before after` empty. The proof
   succeeded; nothing about the compose payload changed.
3. **Apply run 2 (the idempotency proof)** — `changed=1`. The compose task recreated
   **victorialogs and vector**, both with new IDs.
4. **Apply run 3** — `ok=19 changed=0 failed=0`, container IDs stable. Final IDs:
   vector container `1c632b6c7513`, victorialogs container `be0d8cbeeca0`, all healthy.

### Why the usual suspects are innocent

Run 2's recreate survives every explanation available from the repo or the registry, in
the same way the original #85 recreates did:

- **No image moved.** VictoriaLogs' image identifier was `47b820890d64` both before and
  after; the vector image in use was a 14 July build. `pull: always` — which the shared
  deploy role sets for every stack — did not pick up a newer upstream image for either.
  (That identifier is a short image ID, not a `sha256:` content digest; it is strong
  enough to show the image did not change, and it is what was actually captured.)
- **No payload changed.** The compose content and rendered `.env` were identical between
  runs 2 and 3 — and run 3 changed nothing, which is the control: the same payload
  applied to a settled host is a no-op.
- **The change under review was already applied.** Run 1 had converged vector; run 2 had
  nothing of #82's left to do.

So neither the diff nor an image bump explains run 2. The only thing that happened between
run 1 and run 2 was the CLI `up`.

### Inferred mechanism (same epistemic footing as the section above)

The module and the CLI appear to leave subtly different recorded project state, so the
module's next `up` reconciles once and recreates what it cannot match. Note that `up` on a
single service also processes that service's `depends_on` dependencies, so a CLI command
naming one service is not necessarily scoped to one container's bookkeeping.

Be careful about how far that goes. What the evidence establishes is the *interleave and
its cost*: a CLI `up` ran, and the next module `up` recreated victorialogs with vector
following it via `depends_on`. What it does **not** establish is a per-container lineage
snapshot taken between the two — nobody recorded which container the CLI had re-registered
before run 2 fixed it. So this is an inference from observed behaviour, not a verified
reading of Compose's internals, and it should not be promoted beyond that on a single
interleave.

What *is* solidly established is the shape of the effect: one extra convergence cycle,
then stability. Run 3 at `changed=0` with stable IDs proves the system converged rather
than oscillating, which is the distinction that matters operationally.

### What this costs, and how to spend it deliberately

One convergence cycle. That is all — but it lands squarely on the idempotency proof, which
is exactly the run whose `changed=0` you were hoping to show. Two consequences:

- **Order the verification steps.** Run CLI-compose verification (force-recreate proofs,
  manual restarts) *after* the idempotency proof, not before it. If the proof genuinely
  needs the CLI first, budget one extra settle run and say so up front.
- **Read the result correctly.** A `changed=1` on the run immediately following a CLI
  compose touch is expected reconciliation, not a failure of the change under review. The
  claim it licenses is conditional: it is only benign if the *next* run is `changed=0` with
  stable IDs. Without that third run you have an anomaly, not an explanation.

Worth one line because it is free validation: the vector disk buffer was still intact
(8 files) after run 2's unplanned recreate. The #82 persistence fix was proved twice —
once by the deliberate force-recreate, once by a recreate nobody asked for.

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
  check whether the role deliberately requests a recreate on a config change, and check
  whether anyone ran a CLI `docker compose` command against the project since the last
  module run. Only then suspect the change under review.
- **Run CLI `docker compose` verification steps after the idempotency proof, not before
  it.** A manual `up` interleaved between Ansible module runs costs one convergence cycle,
  and it will spend that cycle on the very run whose `changed=0` you were trying to
  demonstrate. If the CLI step has to come first, budget an extra settle run.
- **Read a `changed=1` immediately after a manual compose touch as reconciliation, not as
  regression — but only if the next run proves it.** A following run at `changed=0` with
  stable container IDs shows the system converged; without such a run you have an
  unexplained recreate, not an explained one.
- **Convert a container's UTC `Created` to the host's local time before comparing it to
  Watchtower's schedule.** Watchtower's window is configured in local time and
  `docker inspect` reports UTC; the offset is exactly wide enough to attribute an evening
  compose deploy to the next morning's update window. Cross-check against Watchtower's
  scan scope — a container out of scope can never carry Watchtower lineage at all.
- **When you record lineage, record which compose front-end.** "Created by compose" is not
  precise enough to predict survival — the Ansible module and the CLI are not
  interchangeable for this purpose.

Related: #83 (Watchtower update policy — which services are in scope for unattended
updates is exactly what determines which containers acquire Watchtower lineage). See
also `docs/solutions/integration-issues/watchtower-label-enable-scan-scope.md` for how a
container ends up in or out of Watchtower's scan scope in the first place, and
`docs/solutions/conventions/collocating-compose-stacks-into-ansible-roles.md` for the
relocation whose apply surfaced this — its "creating a file in an rsync'd directory costs
one idempotency cycle" finding is the same shape as the refinement above, and the two are
now a short list of known one-cycle causes rather than one.

The refinement came from #82 (Vector disk-buffer persistence). For the observability stack
whose `depends_on` chain fanned that recreate out to vector, see
`docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md`; for the
second-converge idempotency discipline the refinement constrains, see
`docs/solutions/conventions/ansible-change-loop-pitfalls.md`; and for a different recreate
trigger in the same taxonomy (a changed network definition hash) see
`docs/solutions/integration-issues/docker-compose-shared-network-subnet-recreate.md`.

# Immich ROCm ML Readiness (#244) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Follow `.claude/skills/homelab-change-loop/SKILL.md`: branch `fix/244-immich-rocm-prep`, PR title ends `(#244)`, squash merge.

**Goal:** Ready the latent immich role for ROCm machine learning (890M via the `-rocm` ML image), so the day immich deploys its ML runs on the GPU. **Role-only: no host deploys this; live validation explicitly lands with the future immich deployment.**

**Architecture:** The immich role exists at `ansible/roles/services/immich/` but is commented out of every host's `services:` list (deployment decision is separate — the old blockers were resolved in #91). Only the `immich-machine-learning` service in the compose file changes: image flavor, one device, one env var, and deploy-day caveats as comments.

**Tech Stack:** Docker Compose; Immich `-rocm` ML image (bundles ROCm 7.2 + MIGraphX — Immich's only ROCm flavor).

**Spec:** GitHub issue #244; research report `/Users/surge/dev/rocm-10-n5pro-report-v2.md` (Part 4).

## Global Constraints

- CLAUDE.md binds. The role's existing conventions stay untouched: `security_opt` pair, watchtower `enable`+`monitor-only`, the digest-pinned postgres sidecar, the `:release` tag-family lockstep (no `IMMICH_VERSION` var exists in this role — tags ARE the pin; do not introduce one).
- `immich-server` keeps `/dev/dri` only — VAAPI transcode needs no kfd. Only the ML container gains `/dev/kfd`.
- Verified 2026-08-30: `ghcr.io/immich-app/immich-machine-learning:release-rocm` exists (as do `v2.6.0-rocm` … `v2.7.0-rocm`); the `-rocm` image is ≈35 GiB unpacked; MIGraphX compiles models at first inference (minutes of 100% GPU); gfx1150 is native in Immich ≥v2.6.0 (`release` floats well above that).

---

### Task 1: Compose change

**Files:**
- Modify: `ansible/roles/services/immich/files/compose.yaml` (the `immich-machine-learning` service only, currently lines ~46–72)

- [ ] **Step 1:** Change the ML service to:

```yaml
  immich-machine-learning:
    container_name: immich-machine-learning
    # ROCm ML flavour (#244): same release-family lockstep as immich-server's
    # `:release` (this role pins by tag family — no version var). The -rocm
    # image bundles ROCm 7.2 + MIGraphX and is Immich's ONLY ROCm flavour;
    # kernel-side /dev/kfd is userspace-version-independent of the CT's ROCm 10
    # (#240). Deploy-day caveats, read BEFORE enabling immich on a host:
    #   - the image is ~35 GiB unpacked — check CT 201 rootfs (`df -h /`)
    #     first; root_disk_size in host_vars/n5pro is grow-only live-reconciled
    #   - first inference compiles models via MIGraphX: minutes of 100% GPU is
    #     startup, not a hang
    image: ghcr.io/immich-app/immich-machine-learning:release-rocm
    restart: unless-stopped
    security_opt:
      - "apparmor:unconfined"
      - "no-new-privileges:true"
    labels:
      # Stateless itself, but Immich requires server and ML to run the same release —
      # auto-updating one alone breaks lockstep. Reported, never auto-updated; it moves
      # together with immich-server on a deliberate deploy (#83).
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.monitor-only=true"
    environment:
      MACHINE_LEARNING_WORKERS: 1
      MACHINE_LEARNING_WORKER_TIMEOUT: 120
      # Known -rocm quirk: a loaded model holds the 890M out of its deepest
      # idle state; unload after 5 idle minutes instead of holding VRAM forever.
      MACHINE_LEARNING_MODEL_TTL: 300
    volumes:
      - immich-model-cache:/cache
    devices:
      - /dev/dri:/dev/dri  # VAAPI
      - /dev/kfd:/dev/kfd  # ROCm compute for the -rocm ML image (#244)
    networks:
      - immich_network
    healthcheck:
      test: ["CMD-SHELL", "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:3003/ping\")'"]
      interval: 30s
      timeout: 10s
      retries: 3
```

(Everything outside the comment block, image line, `MACHINE_LEARNING_MODEL_TTL`, and the `/dev/kfd` device line is byte-identical to the current file — preserve it exactly; do not touch the other three services.)

- [ ] **Step 2:** `git diff` — confirm the delta is exactly: image tag, the comment block, one env var + its comment, one device line + comment tweak. Nothing else.

### Task 2: Validate + PR

- [ ] **Step 1:** `task ci:local` — lint, syntax, `scripts/validate-compose.sh` all green (the validator parses this compose even though the role is latent).
- [ ] **Step 2:** `task deploy:services -- --check --diff` — expect NO change on any deployed host (immich is in no `services:` list). Any queued change means a wiring mistake — stop.
- [ ] **Step 3: Commit** — `git commit -m "feat(immich): ready ML for ROCm — release-rocm image, /dev/kfd, model TTL (#244)"`
- [ ] **Step 4:** PR body must state: role-only, nothing deployed, live GPU validation lands with the future immich deployment (rootfs pre-check + MIGraphX warm-up are that deployment's steps). Squash-merge, close #244.

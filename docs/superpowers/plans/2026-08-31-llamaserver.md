# llamaserver — Qwen3.8-27B OpenAI API on the 890M (#241) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Also follow `.claude/skills/homelab-change-loop/SKILL.md`: branch `fix/241-llamaserver`, PR title ends `(#241)`, squash merge.
>
> **BLOCKED until the unblock criterion in Task 0 is met. Do not start Tasks 1+ before Task 0 passes.**

**Goal:** Deploy `llama-server` on CT 201 serving Qwen3.8-27B Q4_K_M from the 890M's 32GB BIOS carveout, exposing an OpenAI-compatible API on host port 8090 to allowlisted LAN consumers, with hard deploy-time evidence the GPU is doing the work.

**Architecture:** Standard `services/<svc>` role deployed by `services/_deploy`; the first compose service in the fleet to pass `/dev/kfd`. Model weights live in the BIOS carveout (outside the CT's RAM cgroup); on gfx1150 `hipMalloc` cannot spill to GTT (ROCm/ROCm#5944), so the carveout is a hard ceiling — which also makes the VRAM-usage assert unambiguous evidence.

**Tech Stack:** Docker Compose, llama.cpp `llama-server` (official ROCm image, tag pinned at unblock), nftables via `nft_scoped_fw`, bartowski GGUF.

**Spec:** GitHub issue #241; session plan `/Users/surge/.claude/plans/soft-wandering-pine.md`; research report `/Users/surge/dev/rocm-10-n5pro-report-v2.md`.

## Global Constraints

- CLAUDE.md binds — in particular: every compose service needs `security_opt: ["apparmor:unconfined", "no-new-privileges:true"]` and a watchtower posture (this one: `enable` + `monitor-only`); `.env` values single-quoted; subnet via `mandatory()`; `get_url` downloads carry `checksum:` with NO stat-exists gate; verify from a BLOCKED source, not just an allowed one; modules return on issuance, not service — gate on the app's own output.
- **Subnet 172.30.0.0/24 is the LAST free n5pro pin** — claiming it must update the fleet-map comment and `docs/architecture.md` in the same change.
- Model: `Qwen3.8-27B-Q4_K_M.gguf`, 17,772,537,440 bytes, sha256 `e103abf9d914d1d7b2f2592f055f2759a71195c350a01c135f71aaae86bca52b` (HF lfs oid, verified 2026-08-30 — re-verify in Task 1 if HF re-uploaded).
- Ports: container **8080** (the official image's baked-in HEALTHCHECK curls `localhost:8080/health`), host **8090** (host 8080 belongs to the latent nextcloud stack).
- No `group_add`: the official image runs as root and the device nodes are root-owned 0660. Named groups would resolve against the IMAGE's `/etc/group`; numeric GIDs would hardcode the host's render GID — both are traps, skip them.
- Never add `--no-mmap` (it would anon-allocate 16.6GB inside CT 201's 24GB cgroup; mmap page cache is reclaimable). CT 201 memory stays 24576.

---

### Task 0: Unblock gate + image pin

- [ ] **Step 1: Verify the unblock criterion** — an *official* llama.cpp ROCm image bundling ROCm **≥10.x** with **gfx1150** in GPU targets:
  - `curl -s https://raw.githubusercontent.com/ggml-org/llama.cpp/master/.devops/rocm.Dockerfile | grep -E 'ROCM_VERSION|gfx1150'` — base was `rocm/dev-ubuntu-24.04:7.2.1-complete` on 2026-08-30; criterion met when the ROCm version is ≥10 and gfx1150 is still targeted;
  - or Docker Hub `rocm/llama.cpp` publishes a ROCm-10.x server tag (was: stops at 7.0).
  If unmet: STOP, comment the check date + findings on #241.
- [ ] **Step 2: Pin the concrete tag** — pick the current per-build server tag (pattern on ghcr: `server-rocm-b<build>`; verify it pulls: `docker manifest inspect ghcr.io/ggml-org/llama.cpp:server-rocm-b<build>`). Confirm in its Dockerfile: runs as root (no `USER`), `curl` present, HEALTHCHECK on `:8080/health`. Record tag + evidence on #241. This tag replaces `<PINNED-TAG>` in Task 2.
- [ ] **Step 3: Rootfs pre-flight:** `ssh root@n5pro-docker.lan 'df -h /'` — the image is roughly 7–8GB compressed / 15–20GB unpacked and a pull needs ~both transiently. If free < ~30GB: bump `root_disk_size` for CT 201 in `ansible/inventory/host_vars/n5pro/vars.yml` (grow-only, live-reconciled by `pct resize` in proxmox_guests) and apply `task infra:hosts -- --limit n5pro` as a first commit.

### Task 1: Role files

**Files:**
- Create: `ansible/roles/services/llamaserver/files/compose.yaml`
- Create: `ansible/roles/services/llamaserver/templates/env.j2`
- Create: `ansible/roles/services/llamaserver/defaults/main.yml`
- Create: `ansible/roles/services/llamaserver/README.md`

**Interfaces:**
- Consumes: `docker_networks.llamaserver` and `llamaserver_firewall` from host_vars (Task 3); `data_mount` (global, `/data`).
- Produces: env vars `LLAMASERVER_MODEL_DIR`, `LLAMASERVER_NETWORK_SUBNET` (compose `${...:?}`); tunables `LLAMASERVER_CTX_SIZE`, `LLAMASERVER_PARALLEL` (compose `${...:-default}`); model served under alias `qwen3.8-27b`.

- [ ] **Step 1: `files/compose.yaml`:**

```yaml
services:
  llamaserver:
    container_name: llamaserver
    # PINNED build tag, deliberately not the rolling `server-rocm`: _deploy runs
    # `pull: always`, so a rolling tag would move the inference server to a new
    # llama.cpp master build on every deploy. Bumps are git edits of this line
    # (#241; same posture as the watchtower pin).
    image: ghcr.io/ggml-org/llama.cpp:<PINNED-TAG>
    restart: unless-stopped
    security_opt:
      - "apparmor:unconfined"
      - "no-new-privileges:true"
    labels:
      # Big-model runtime: enable + monitor-only per the Watchtower posture
      # table (#83) — counted and reported, never auto-recreated mid-inference.
      # With the tag pinned, the only possible report is a re-push of that tag.
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.monitor-only=true"
    devices:
      # First /dev/kfd consumer in the fleet. The image runs as root (no USER in
      # .devops/rocm.Dockerfile) and the nodes are root-owned 0660, so no
      # group_add is needed — named groups would resolve against the IMAGE's
      # /etc/group and numeric GIDs would hardcode the host's render GID.
      - /dev/kfd:/dev/kfd
      - /dev/dri:/dev/dri
    ports:
      - "8090:8080"
    volumes:
      - ${LLAMASERVER_MODEL_DIR:?}:/models:ro
    command:
      - --model
      - /models/Qwen3.8-27B-Q4_K_M.gguf
      - --alias
      - qwen3.8-27b
      - --host
      - 0.0.0.0
      - --port
      - "8080"
      - --n-gpu-layers
      - "999"
      - --ctx-size
      - ${LLAMASERVER_CTX_SIZE:-32768}
      - --parallel
      - ${LLAMASERVER_PARALLEL:-1}
      - --metrics
    healthcheck:
      # Same probe as the image's baked-in HEALTHCHECK, redeclared to add
      # start_period: /health returns 503 while the 16.6 GB model loads.
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10m
    networks:
      - llamaserver_network

networks:
  llamaserver_network:
    name: llamaserver_network
    ipam:
      config:
        - subnet: ${LLAMASERVER_NETWORK_SUBNET:?llamaserver subnet must come from host_vars docker_networks}
```

- [ ] **Step 2: `templates/env.j2`** (copy the boilerplate header comment from `roles/services/vaultwarden/templates/env.j2`; every value single-quoted, #117):

```jinja
LLAMASERVER_MODEL_DIR='{{ data_mount }}/llamaserver/models'
LLAMASERVER_NETWORK_SUBNET='{{ docker_networks.llamaserver | mandatory('docker_networks.llamaserver must be pinned in host_vars — see the fleet map in host_vars/n5pro_docker/vars.yml') }}'
```

(`LLAMASERVER_CTX_SIZE`/`LLAMASERVER_PARALLEL` carry `:-` defaults in compose, so `scripts/validate-compose.sh` does not require them here — they stay tunable without churn.)

- [ ] **Step 3: `defaults/main.yml`:**

```yaml
---
# Fail-closed: {} routes a host that enables the role without an allowlist into
# nft_scoped_fw's non-empty-mapping assert (portainer precedent).
llamaserver_firewall:
  ports: {}

llamaserver_model_url: "https://huggingface.co/bartowski/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-Q4_K_M.gguf"
llamaserver_model_file: "Qwen3.8-27B-Q4_K_M.gguf"
# lfs oid from https://huggingface.co/api/models/bartowski/Qwen3.8-27B-GGUF/tree/main (2026-08-30)
llamaserver_model_sha256: "e103abf9d914d1d7b2f2592f055f2759a71195c350a01c135f71aaae86bca52b"
```

- [ ] **Step 4: `README.md`** — one page: what it serves (OpenAI-compatible API at `http://n5pro-docker.lan:8090/v1`, alias `qwen3.8-27b`), how consumers authenticate (no auth — network allowlist is the boundary; add clients by CIDR in `llamaserver_firewall`), the image-pin bump procedure, and the ctx/parallel tunables.
- [ ] **Step 5: Commit** — `git commit -m "feat(llamaserver): role files — first /dev/kfd compose service (#241)"`

### Task 2: tasks/main.yml — firewall, model download, deploy, delivered-service gates

**Files:**
- Create: `ansible/roles/services/llamaserver/tasks/main.yml`

**Interfaces:**
- Consumes: `nft_scoped_fw` role (`nft_fw_name`, `nft_fw_description`, `nft_fw_ports` — see `roles/services/portainer/tasks/main.yml` for the include shape); `services/_deploy` (`svc`, `svc_data_dirs`).

- [ ] **Step 1: Write the flow:**

```yaml
---
- name: Restrict the llama-server API port to approved sources
  ansible.builtin.include_role:
    name: nft_scoped_fw
  vars:
    nft_fw_name: llamaserver
    nft_fw_description: llama-server API firewall (Ansible-managed nftables table inet llamaserver_fw)
    nft_fw_ports: "{{ llamaserver_firewall.ports }}"

- name: Create llamaserver model directory
  ansible.builtin.file:
    path: "{{ data_mount }}/llamaserver/models"
    state: directory
    mode: "0755"

# 16.6 GB, first run only. get_url with checksum and NO stat-exists gate
# (CLAUDE.md): later runs re-hash the on-disk file and skip the fetch; a
# truncated or corrupted download fails HERE, not as a llama-server parse
# error at 03:00.
- name: Download Qwen3.8-27B Q4_K_M weights
  ansible.builtin.get_url:
    url: "{{ llamaserver_model_url }}"
    dest: "{{ data_mount }}/llamaserver/models/{{ llamaserver_model_file }}"
    checksum: "sha256:{{ llamaserver_model_sha256 }}"
    mode: "0644"
    timeout: 120
  when: not ansible_check_mode   # --check cannot honestly preview a 16 GB fetch

- name: Deploy llamaserver via the shared service pipeline
  ansible.builtin.include_role:
    name: services/_deploy
  vars:
    svc: llamaserver
    svc_data_dirs:
      - "{{ data_mount }}/llamaserver/models"

# --- Delivered-service gates: modules return on issuance, not service --------
# /health is 503 through the multi-minute model load; 200 = loaded + serving.
- name: Wait for llama-server to finish loading the model
  ansible.builtin.uri:
    url: http://localhost:8090/health
    status_code: 200
  register: llamaserver_health
  until: llamaserver_health is succeeded
  retries: 60
  delay: 10
  changed_when: false
  when: not ansible_check_mode

# GPU evidence 1: full offload, from llama-server's own load log. The backref
# makes 'offloaded 48/48' pass and 'offloaded 0/48' fail. Guarded on the load
# lines still being in the log (json-file rotation ages them out of a
# long-lived container); the VRAM floor below is the rotation-proof guard.
- name: Read llama-server startup log
  ansible.builtin.command: docker logs --tail 2000 llamaserver
  register: llamaserver_log
  changed_when: false
  when: not ansible_check_mode

- name: Assert every layer was offloaded to the GPU
  ansible.builtin.assert:
    that:
      - (llamaserver_log.stdout ~ llamaserver_log.stderr)
        is search('offloaded (\d+)/\1 layers to GPU')
    fail_msg: >-
      llama-server's load log does not show a full N/N GPU offload — either
      layers fell back to CPU (check ROCm/kfd inside the container) or the
      load lines rotated out; `docker logs llamaserver | grep offloaded`
      shows which.
    quiet: true
  when:
    - not ansible_check_mode
    - "'load_tensors' in (llamaserver_log.stdout ~ llamaserver_log.stderr)"

# GPU evidence 2, rotation-proof: the weights are 16.55 GiB — if they are on
# the GPU, VRAM used cannot be under 15 GiB while the service is healthy, and
# on gfx1150 they cannot be hiding in GTT (ROCm/ROCm#5944: hipMalloc has no
# GTT reach), so a low number means CPU inference. Do not ship that.
- name: Read GPU VRAM usage (CT-side ROCm userspace, #240)
  ansible.builtin.command: /opt/rocm/core-10.0/bin/rocm-smi --showmeminfo vram --json
  register: llamaserver_vram
  changed_when: false
  when: not ansible_check_mode

- name: Assert the model is resident in VRAM
  ansible.builtin.assert:
    that:
      - (llamaserver_vram.stdout | from_json).card0['VRAM Total Used Memory (B)'] | int
        > 15 * 1024 * 1024 * 1024
    fail_msg: >-
      llama-server is healthy but GPU VRAM used is below the 15 GiB floor the
      16.55 GiB weights require — CPU inference or a probe failure.
      Raw: {{ llamaserver_vram.stdout | default('unreadable') }}
    quiet: true
  when: not ansible_check_mode

# Delivered API: an actual OpenAI-shaped completion returning tokens.
- name: Prove an OpenAI-compatible completion returns tokens
  ansible.builtin.uri:
    url: http://localhost:8090/v1/chat/completions
    method: POST
    body_format: json
    body:
      model: qwen3.8-27b
      messages:
        - role: user
          content: "Reply with the single word OK."
      max_tokens: 8
    return_content: true
  register: llamaserver_completion
  changed_when: false
  when: not ansible_check_mode

- name: Assert the completion carried content
  ansible.builtin.assert:
    that:
      - llamaserver_completion.json.choices[0].message.content | length > 0
    fail_msg: "llama-server answered but returned no tokens: {{ llamaserver_completion.json | default({}) }}"
    quiet: true
  when: not ansible_check_mode
```

- [ ] **Step 2: Pin the real rocm-smi JSON key before trusting the VRAM assert** — on CT 201 run `/opt/rocm/core-10.0/bin/rocm-smi --showmeminfo vram --json` and read the actual key names (`card0` / `VRAM Total Used Memory (B)` are the ROCm-7-era shapes and may have changed in 10.x; `amd-smi` is the fallback CLI). Fix the assert's key string to what the live output shows, and note the observed shape in a comment.
- [ ] **Step 3: Commit** — `git commit -m "feat(llamaserver): deploy pipeline with delivered-GPU gates (#241)"`

### Task 3: host_vars wiring + docs

**Files:**
- Modify: `ansible/inventory/host_vars/n5pro_docker/vars.yml`
- Modify: `docs/architecture.md`

- [ ] **Step 1:** In `host_vars/n5pro_docker/vars.yml`: add `llamaserver` to the `services:` list (after `watchtower`); add to `docker_networks:` → `llamaserver: "172.30.0.0/24"`; update the fleet-map comment (the "172.30 unassigned — next free n5pro pin" line goes away; note the n5pro range 172.26–172.31 is now FULL and the next new n5pro service must extend the map into 172.16–172.17); add:

```yaml
llamaserver_firewall:
  ports:
    8090:
      - 192.168.48.0/24   # operator workstation subnet
      - 192.168.25.15/32  # eq12-docker CT 101 — API consumers + telegraf /health probe (#243)
```

- [ ] **Step 2:** `docs/architecture.md`: add the network-map row (`llamaserver | llamaserver_network | 172.30.0.0/24`), delete/replace the "172.30.0.0/24 is unassigned — the next free n5pro pin" sentence, add port 8090 to the N5 Pro port reference and llamaserver to the service placement table.
- [ ] **Step 3:** `task ci:local` — lint, syntax, and `scripts/validate-compose.sh` (cross-checks compose `${VAR}` against env.j2) all green.
- [ ] **Step 4: Commit** — `git commit -m "feat(llamaserver): wire into n5pro_docker — claim the last free subnet pin (#241)"`

### Task 4: Deploy + verify (live)

- [ ] **Step 1:** `task deploy:service -- --tags llamaserver --limit n5pro_docker --check --diff` — judge the delta (a predicted Recreate is not a real one).
- [ ] **Step 2:** Live: `task deploy:service -- --tags llamaserver --limit n5pro_docker`. First run downloads 16.6GB then loads for minutes — the health gate's 60×10s window covers the load, not the download (get_url blocks synchronously; expect a long task).
- [ ] **Step 3: Prove the gates can fail** (each once, then revert):
  - VRAM assert: re-run play with `-e` override is not available for a literal — instead temporarily edit the threshold to `> 100 * 1024**3`, run the gates portion (`task deploy:service -- --tags llamaserver --limit n5pro_docker`), expect RED, revert.
  - Completion assert: POST with `model: bogus-alias` manually (`curl -s -X POST http://localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"bogus","messages":[{"role":"user","content":"x"}]}'` from CT 201) and confirm the server rejects/errors — evidence the assert's happy path is meaningful.
- [ ] **Step 4: Cross-host + firewall evidence** (paste into #241):
  - From an allowlisted workstation: `curl -s http://192.168.30.15:8090/v1/models` → JSON listing `qwen3.8-27b`.
  - From a NON-allowlisted source (e.g. a container/host outside both CIDRs): connection refused/timeout — verify from a BLOCKED source.
  - `ssh root@n5pro-docker.lan 'nft list table inet llamaserver_fw'` → 8090 rules with both CIDRs.
  - `ssh root@n5pro-docker.lan 'docker inspect --format "{{.State.Health.Status}}" llamaserver'` → `healthy`.
- [ ] **Step 5: Idempotence:** second `task deploy:service -- --tags llamaserver --limit n5pro_docker` — model download skipped (hash match), container not recreated (compare container ID + `.Created` before/after), all gates green.

### Task 5: PR + merge

- [ ] **Step 1:** `gh pr create --title "feat(llamaserver): OpenAI-compatible Qwen3.8-27B on the 890M (#241)"` — body links the evidence comments; note the operator should confirm the firewall consumer set before merge.
- [ ] **Step 2:** Squash-merge, delete branch, confirm #241 closes. Unblocks #242 (benchmark) and #243 (probe).

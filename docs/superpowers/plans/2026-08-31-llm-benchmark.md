# llamaserver GPU Benchmark + MTP Decision (#242) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Follow `.claude/skills/homelab-change-loop/SKILL.md`: branch `fix/242-llm-benchmark`, PR title ends `(#242)`, squash merge.
>
> **Blocked on #241** — llamaserver must be deployed and healthy before starting.

**Goal:** Measure Qwen3.8-27B Q4_K_M inference on the 890M under ROCm (pp512/tg128, API-level tokens/s, VRAM and power), decide whether the MTP speculative-decode flags become permanent, and record the numbers that inform the deferred BIOS-carveout decision.

**Architecture:** A one-time measured drill (no durable Ansible asserts — throughput varies with load; the durable gates live in #241's role). Results land in `docs/n5pro.md`. The only possible code change is adding two flags to the llamaserver compose command.

**Tech Stack:** llama.cpp (`llama-bench` via the toolbox image, `llama-server /metrics`), rocm-smi, Grafana/VictoriaMetrics (`sensors_power1_average{chip="amdgpu-pci-c700"}` — already collected host-side).

**Spec:** GitHub issue #242; research report `/Users/surge/dev/rocm-10-n5pro-report-v2.md` (Part 2: carveout economics; ~11% GTT penalty; Vulkan-flip alternative).

## Global Constraints

- CLAUDE.md experiment rule binds hard here: **before believing a measurement, write down what result would falsify the alternative** — and run measurements that can actually discriminate (e.g. power RISE under load vs idle baseline, not just "a number exists").
- Baselines FIRST, with the service stopped — "measure the baseline before claiming a win".
- MTP facts (verified 2026-08-30): flags `--spec-type draft-mtp --spec-draft-n-max 2` (llama.cpp PR #22673); claimed +33–39% decode; benefit gone by ~4 parallel slots → only valid with `--parallel 1`; nextn tensors confirmed for unsloth GGUFs, **unconfirmed for bartowski**.
- gfx1150 `hipMalloc` cannot reach GTT (ROCm/ROCm#5944) — the carveout is a hard ceiling; that is what makes the headroom arithmetic meaningful.
- Read-only SSH is fine throughout; the only state changes are stopping/starting llamaserver (announce to the operator first — it interrupts API consumers) and the optional compose edit.

---

### Task 1: Baselines (service stopped)

- [ ] **Step 1:** Tell the operator llamaserver will be stopped for the drill window; get an OK.
- [ ] **Step 2:** `ssh root@n5pro-docker.lan 'docker stop llamaserver'` then capture:
  - `ssh root@n5pro-docker.lan '/opt/rocm/core-10.0/bin/rocm-smi --showmeminfo vram'` → idle VRAM-used (record exact bytes).
  - Idle GPU power: query VictoriaMetrics for the last 15 min of `sensors_power1_average{host="n5pro", chip="amdgpu-pci-c700"}` (via the observability stack's VM API or the Grafana n5pro dashboard panel "iGPU power and clock"); record the idle wattage. Falsifier note: if load-power later does NOT rise above this baseline, the "GPU is doing the work" claim fails regardless of throughput numbers.
- [ ] **Step 3:** While stopped, note this window as the #243 probe-failure evidence opportunity (the probe series must go non-zero) — coordinate if #243 is already deployed.

### Task 2: llama-bench (pp512 / tg128)

- [ ] **Step 1:** Confirm the toolbox image tag matching the pinned server tag exists (same build suffix, `full-` variant): `docker manifest inspect ghcr.io/ggml-org/llama.cpp:full-rocm-b<build>`; the full image wraps tools behind an entrypoint — check `docker run --rm <image> --help` for how it exposes `llama-bench` (historically `--run`/tool-name arguments; use what the help says).
- [ ] **Step 2:** Run on CT 201 (service still stopped — the bench needs the VRAM):

```bash
docker run --rm --device /dev/kfd --device /dev/dri \
  --security-opt apparmor=unconfined \
  -v /data/llamaserver/models:/models:ro \
  ghcr.io/ggml-org/llama.cpp:full-rocm-b<build> \
  <entrypoint-args-for> llama-bench -m /models/Qwen3.8-27B-Q4_K_M.gguf -ngl 999
```

Record the pp512 and tg128 rows (t/s ± stddev) and the device line (must name gfx1150/ROCm — if it says CPU, stop: nothing below is valid).

### Task 3: MTP probe + decode comparison

- [ ] **Step 1:** Probe the GGUF for MTP tensors: `docker run --rm -v /data/llamaserver/models:/models:ro <full image> <args-for> llama-gguf /models/Qwen3.8-27B-Q4_K_M.gguf` (or `gguf-dump` from a python `gguf` package) — look for `blk.*.nextn.*` tensor names. If ABSENT: record "MTP unavailable in bartowski quant" in the results table, skip Step 2, and note the alternative (unsloth quant carries them) as a possible future model swap.
- [ ] **Step 2 (only if present):** Start llamaserver (`docker start llamaserver`, wait healthy), run a timed generation batch (Task 4's command) as the no-MTP measurement; then temporarily add to the compose command in the DEPLOYED copy? — no: never mutate deploy dirs by hand. Instead run a second, throwaway server container with the MTP flags for the comparison:

```bash
docker run --rm --device /dev/kfd --device /dev/dri \
  --security-opt apparmor=unconfined \
  -v /data/llamaserver/models:/models:ro -p 18080:8080 \
  ghcr.io/ggml-org/llama.cpp:server-rocm-b<build> \
  --model /models/Qwen3.8-27B-Q4_K_M.gguf --alias qwen-mtp --host 0.0.0.0 --port 8080 \
  --n-gpu-layers 999 --ctx-size 32768 --parallel 1 --metrics \
  --spec-type draft-mtp --spec-draft-n-max 2
```

(Stop the main llamaserver first — the carveout cannot hold two copies.) Measure the same generation batch against `:18080`; read draft-acceptance stats from `/metrics`. Falsifier: MTP is only adopted if measured decode t/s improves ≥10% on the same prompt set — otherwise record and decline.

### Task 4: API-level tokens/s + power under load

- [ ] **Step 1:** With the production llamaserver healthy, from an allowlisted host run a timed non-stream completion 3× and read the server-reported timings:

```bash
time curl -s http://192.168.30.15:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Write 300 words about ZFS."}],"max_tokens":400,"stream":false}' \
  | jq '{usage, timings}'
```

Record predicted_per_second (or tokens/elapsed) per run.

- [ ] **Step 2:** During a sustained generation (repeat the request in a loop for ~5 min), re-query `sensors_power1_average{host="n5pro", chip="amdgpu-pci-c700"}` — the load value must be clearly above Task 1's idle baseline. Record both numbers. This is the cross-layer GPU-use evidence (the host sensor cannot be faked by container-side reporting).
- [ ] **Step 3:** Record VRAM used at the deployed 32k ctx (`rocm-smi --showmeminfo vram` while healthy + idle-loaded).

### Task 5: Record results + carveout conclusion

**Files:**
- Modify: `docs/n5pro.md` (new section after the GPU Passthrough section)
- Maybe modify: `ansible/roles/services/llamaserver/files/compose.yaml` (MTP adoption only)

- [ ] **Step 1:** Add `## LLM inference benchmarks (llamaserver)` to `docs/n5pro.md` with: date, image tag, model+quant, the table (pp512, tg128, MTP on/off, API tokens/s, VRAM at 32k ctx, idle vs load power), and the **carveout conclusion**: `32GB − (VRAM used at 32k ctx) = headroom`; state how small the BIOS carveout could go while still fitting weights+KV (+~1GB transcode surfaces for future Immich), and reference the report's alternative (8GB carveout + GTT + Vulkan flip, ~11% tg penalty but ~24GB RAM back) as the standing option this data feeds.
- [ ] **Step 2 (conditional):** If MTP won in Task 3: append to the compose `command:` list:

```yaml
      # MTP speculative decode: measured +<N>% tg on <date> (#242). Only valid
      # with --parallel 1 — the benefit is gone by ~4 concurrent slots.
      - --spec-type
      - draft-mtp
      - --spec-draft-n-max
      - "2"
```

Redeploy (`task deploy:service -- --tags llamaserver --limit n5pro_docker`) and confirm the #241 gates stay green.
- [ ] **Step 3:** Commit(s): `docs(n5pro): LLM inference benchmark results (#242)` and, if adopted, `feat(llamaserver): enable MTP speculative decode — measured +<N>% (#242)`.
- [ ] **Step 4:** PR, squash-merge, close #242 with the results table in a closing comment. If the carveout conclusion recommends action, open a follow-up issue for the BIOS/GTT decision citing the numbers.

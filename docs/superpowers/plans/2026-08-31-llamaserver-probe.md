# LAN Health Probe for llamaserver (#243) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Follow `.claude/skills/homelab-change-loop/SKILL.md`: branch `fix/243-llamaserver-probe`, PR title ends `(#243)`, squash merge.
>
> **Blocked on #241** — the probe target must exist and be healthy.

**Goal:** Continuous liveness for the llamaserver OpenAI API: one `http_response` probe from the eq12 observability telegraf, riding the existing probe/absence alert rules.

**Architecture:** The observability stack's telegraf (eq12_docker) already runs `[[inputs.http_response]]` against a urls list; alerts aggregate `by (server, check_type)` and the absence rule is per-series, so a new URL self-registers — no new alert rules. `/health` is 200 only when the model is loaded and serving, so still-loading and crash-looping both read as down (that's the point).

**Tech Stack:** telegraf `inputs.http_response`, VictoriaMetrics, existing `obs-http-probe-*` Grafana rules.

**Spec:** GitHub issue #243.

## Global Constraints

- CLAUDE.md binds. In particular: a guard/probe you have not seen fail is not a guard — Task 3 must observe the probe red once.
- Probe egress is the observability container on CT 101, NAT'd to **192.168.25.15** — already allowlisted in `llamaserver_firewall` (#241). If #241's allowlist changed, verify before deploying.
- Cross-host probes use LAN URLs, never compose service names (SearXNG-via-gateway precedent in the same file).

---

### Task 1: Add the probe URL

**Files:**
- Modify: `ansible/roles/services/observability/files/data/telegraf/telegraf.conf` (the `[[inputs.http_response]]` urls list, ~lines 301–321)
- Modify: `ansible/roles/services/observability/tasks/main.yml` (~line 630 — stale url-count prose)

- [ ] **Step 1:** Add to the `urls` list:

```toml
    # llamaserver on n5pro-docker via LAN (#241/#243). /health is 200 only when
    # the model is loaded AND serving — still-loading or crash-looping reads as
    # down, which is the point. Probe egress (CT 101, 192.168.25.15) is
    # allowlisted in llamaserver_firewall (host_vars/n5pro_docker).
    "http://192.168.30.15:8090/health",
```

- [ ] **Step 2:** Fix the comment in `roles/services/observability/tasks/main.yml` (~line 630) that counts the http_response urls ("parses to 4" → the new count). Read the surrounding task first — if it is an assert on the count rather than prose, update the asserted number the same way.
- [ ] **Step 3:** `task ansible:lint && task ansible:syntax` — clean.
- [ ] **Step 4: Commit** — `git commit -m "feat(observability): LAN health probe for llamaserver (#243)"`

### Task 2: Deploy + positive evidence

- [ ] **Step 1:** `task deploy:service -- --tags observability --check --diff` then live: `task deploy:service -- --tags observability`.
- [ ] **Step 2:** Wait ~2 probe intervals, then query VictoriaMetrics (from an allowlisted source, with the vm auth from the observability role's env; note VM ingestion isn't instantly queryable — allow a minute):

```bash
curl -su "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" \
  'http://192.168.25.15:8428/api/v1/query?query=http_response_result_code{server="http://192.168.30.15:8090/health"}'
```

Expected: the series exists with value `0` (success). Paste into #243.

### Task 3: Prove the probe can fail

- [ ] **Step 1:** Coordinate with the #242 drill's stop window if it hasn't happened yet (one stop serves both); otherwise announce and briefly `docker stop llamaserver` on CT 201.
- [ ] **Step 2:** Re-run the Task 2 query — `http_response_result_code` for the server must go non-zero (or `result_type` change), and the existing `obs-http-probe-*` alert should reflect it if the stop outlasts the rule window (don't hold the outage just to page — the series changing is the evidence).
- [ ] **Step 3:** `docker start llamaserver`, wait healthy, confirm the series returns to 0. Paste both readings into #243.
- [ ] **Step 4:** PR (`feat(observability): LAN health probe for llamaserver (#243)`), squash-merge, close #243.

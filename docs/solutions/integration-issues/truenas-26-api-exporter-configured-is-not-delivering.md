---
title: "TrueNAS 26 metrics: a configured reporting exporter is not a delivering one, and graphite measurement names collide with telegraf's own"
date: 2026-08-23
category: integration-issues
module: observability
problem_type: integration_issue
component: tooling
symptoms:
  - "ansible-playbook reports changed=1 and the exporter reads back correctly, but nothing arrives in VictoriaMetrics"
  - "nftables drop counter stays at 0 and ss shows no connection to :2003 — the appliance never even tries"
  - "TrueNAS metrics carry host=homelab-telegraf, the collector's name, not the NAS"
  - "truenas_disk_temp appears empty over a 5-minute window while other TrueNAS series report every 60s"
  - "module failure returns only {\"censored\": \"the output has been hidden...\"}"
  - "sudo: a password is required, on a playbook that touches no remote host"
root_cause: api_behavior
resolution_type: config_change
severity: medium
related_components:
  - truenas
  - telegraf
  - victoriametrics
  - nftables
---

# TrueNAS 26 metrics into VictoriaMetrics

Six traps met landing #173. Four are TrueNAS-specific; two are Ansible traps that
any `ansible_connection: local` role will hit.

## 1. A configured exporter is not a delivering exporter

`reporting.exporters.create` writes the config and returns success. The exporter
then reads back **exactly correct** — `enabled: true`, right destination, right
port — and **nothing is sent**. netdata does not promptly load the new exporting
config, and the API surface reachable with `REPORTING_WRITE` contains no method
that makes it. Measured: no connection attempt at all for minutes after a green
apply; the nftables drop counter stayed at `0`, which is the proof it was not a
firewall problem — the appliance never sent a packet to drop.

Delivery began only after several minutes and an `enabled` false→true cycle
through the role.

**So a green deploy proves nothing here.** The evidence is a query against
VictoriaMetrics, never the playbook recap, and the durable control is the
ingest-stalled alert rule (#176) rather than a once-per-deploy assert. Same
category as every other silent-green trap in this repo: the thing that reports
success is not the thing that does the work.

## 2. Graphite measurement names collide with telegraf's own inputs

TrueNAS's netdata exports charts called `net.ens18` and `system.load`. The
telegraf that receives them **also runs `inputs.net`, `inputs.system`,
`inputs.cpu`, `inputs.disk` and `inputs.mem` against its own host**. A natural
graphite template maps those onto measurements `net` and `system` — merging two
machines' data under identical metric names. Nothing errors; it shows up only as
impossible values on a dashboard.

Every template must therefore produce a `truenas_`-prefixed measurement, with
`namepass = ["truenas_*"]` re-asserting it. Prefixing here is a correctness
requirement, not a naming preference.

## 3. Metrics are attributed to the collector, not the source

telegraf's `[agent]` block pins `hostname = "homelab-telegraf"`, and the agent
stamps that `host` tag on everything it emits — including metrics it merely
*received* from another machine. Every TrueNAS series claimed to originate from
the collector.

Fix: the exporter's `namespace` carries the appliance's identity, and the
graphite template maps that segment to the **`host` label** — so attribution is
derived from configuration rather than inherited from whoever forwarded the data.
The fleet-wide version of this (eq12_docker's own metrics also claim
`homelab-telegraf`) is filed as #178.

## 4. Disk temperature is SPARSE, and the boot disk lies

`truenas_disk_temp` updates every few **minutes**, not every 60s like the rest of
the stream. A 5-minute query window can legitimately return nothing on a
perfectly healthy array — which is the "absence is not evidence" lesson in its
alerting form. Any rule on it needs a lookback wider than the update interval, or
it flaps to NoData between samples.

And `_devicename_sda` — the virtual boot disk, which has no SMART — reports
**`0`, not null**. A fabricated zero drags down every min/avg panel and can mask a
genuinely hot array. Exclude it explicitly from panels and rules. Note the
asymmetry with the API: `disk.temperatures` returns `null` for the same disk, so
the two sources disagree about how to say "unknown".

## 5. `no_log: true` on a task blinds the failure you added it to catch

Measured: with task-level `no_log`, a failing module returns only
`{"censored": "the output has been hidden..."}` — the credential is protected and
so is the *bug*. Wrong password, unreachable host and rejected field all look
identical.

The right mechanism is `no_log=True` on the **argument_spec parameter**, which
redacts that one value and leaves the error message readable. Verified by
pointing the role at an unreachable address: the failure now names the host and
the exception, with no credential in the output.

## 6. `become = True` in ansible.cfg breaks any local-connection play

`ansible.cfg` sets `become = True` fleet-wide, which is right for every SSH host
here. On an `ansible_connection: local` host it tries to sudo **on the operator's
own laptop** and dies with `sudo: a password is required`. Set `become: false` on
the play.

The sibling trap in the same play: interpreter discovery picks the system python,
not the `uv tool` environment where the API client is installed, so the module
fails to import. Pin `ansible_python_interpreter: "{{ ansible_playbook_python }}"`
— written that way and not as a literal path, because this repo is driven from a
Mac *and* an Arch box and a hardcoded path works on exactly one of them.

## Also worth knowing

- TrueNAS 26.0 **removed the REST API** (`/api/v2.0/*` → 404). The replacement is
  JSON-RPC 2.0 over a WebSocket at `/api/current` (400 on GET, 405 on POST), and
  `ansible.builtin.uri` cannot speak it — hence a custom module.
- `truenas_api_client` is **not on PyPI** and reports version `0.0.0`, so it
  cannot be pinned by version. Pin the git SHA.
- `GRAPHITE` is the only exporter type (`exporter_type` is a schema `const`).
- Two schema defaults are hazards: `matching_charts: "*"` (the whole netdata
  firehose) and `update_every: 1` (one sample per second per dimension).
- `privilege.local_groups` and `user.group` take **database entry IDs, not GIDs**.
- `REPORTING_WRITE` includes `REPORTING_READ`, and least privilege is real:
  `pool.query`, `user.query`, `privilege.query` and `system.reboot` are all denied
  to an account holding only it.

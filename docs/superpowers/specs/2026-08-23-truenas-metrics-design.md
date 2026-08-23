# TrueNAS metrics into VictoriaMetrics — design

**Date:** 2026-08-23
**Issues:** #173 (ingest path), #174 (API poller), #175 (dashboard), #176 (alerting)
**Status:** approved; #173 implementing

## Goal

TrueNAS (VM 200) holds all the bulk storage and is the only machine in the fleet
with no telemetry in VictoriaMetrics. Stream its metrics — disk temperatures
first, plus pool/ZFS state, system vitals and network/NFS throughput — into the
existing observability stack and graph them in a Grafana NAS dashboard.

## Evidence base

Everything below was **measured against the live appliance**, read-only, before
the design was fixed. Nothing here is inferred from documentation alone.

| Fact | How established |
| ---- | --------------- |
| TrueNAS is `192.168.30.20` on vmbr1 | ARP on n5pro; MAC `bc:24:11:2d:84:94` matches VM 200 `net0` |
| Running the **v26.0 API** | `/api/versions` -> `[..., "v26.0.0"]` |
| **REST is gone** | `/api/v2.0/system/version` -> 404 |
| **JSON-RPC 2.0 over WebSocket only** | `/api/current` -> 400 on GET, 405 on POST |
| `GRAPHITE` is the **only** exporter type | `reporting.exporters.exporter_schemas` -> `['GRAPHITE']`; `exporter_type` is a schema `const` |
| No exporter configured yet | `reporting.exporters.query` -> `[]` (first apply is a create) |
| API key auth works for a **password-disabled** account | `auth.login_with_api_key` -> `True`; `auth.me` -> `ansible-ctrl` |
| `REPORTING_WRITE` includes `REPORTING_READ` | `reporting.exporters.query` succeeds with `REPORTING_WRITE` alone |
| **Least privilege is real** | `pool.query`, `user.query`, `privilege.query`, `system.reboot` all **denied** |
| Disk temps available by **device name** | `disk.temperatures` -> `{"sdf":40.0,"sde":39.0,"sdd":39.0,"sdc":38.0,"sdb":40.0,"sda":null,"nvme1n1":33.85,"nvme0n1":33.85}` |
| Nothing exports metrics today | ports 6999/9090/9100/161 closed from outside |

Two schema defaults are **hazards**, confirmed against the box:

- `update_every` defaults to **1** — one sample per second per dimension, into a
  5-year-retention TSDB on an N100.
- `matching_charts` defaults to `*` — the entire netdata firehose.

Both are pinned explicitly. The role refuses to converge an exporter whose
`matching_charts` is `*`.

## Architecture

```
TrueNAS 26.0 (VM 200, 192.168.30.20)
  └─ netdata (internal, localhost:6999)
       └─ reporting.exporters  [GRAPHITE, matching_charts gate #1]
            │  prefix.hostname.chart.dimension, plaintext TCP, update_every=60
            ▼  vmbr1 LAN
eq12_docker (CT 101, 192.168.25.15)
  ├─ nftables scoped allowlist :2003 <- 192.168.30.20/32 ONLY
  └─ telegraf
       ├─ [[inputs.socket_listener]] :2003 graphite  [allowlist gate #2]
       └─ [[inputs.prometheus]] -> truenas-poller:9109/metrics   (#174)
                 ▼
       outputs.http -> prometheusremotewrite -> victoriametrics:8428
                       (authenticated, over compose net 172.20.0.0/24)
                 ▼
       Grafana: NAS dashboard (#175) + 5 alert rules (#176)
```

### Why telegraf and not VictoriaMetrics' own Graphite listener

VictoriaMetrics ships `--graphiteListenAddr` on :2003 and `PORT_REFERENCE.md`
already documents it as not enabled. Pointing TrueNAS at it directly would save a
hop but would recreate exactly what #133 removed: an **unauthenticated raw-socket
write surface** on this host — the `:8089` mistake, rebuilt. It also offers no
per-source filtering (`-relabelConfig` is global, so a TrueNAS-shaped drop rule
would sit in every writer's path) and no way to turn `truenas.<host>.disktemp.<x>`
into a labelled series. Telegraf's graphite parser does both and already holds an
authenticated write path to VM.

### Why eq12_docker and not n5pro_docker

n5pro_docker is one host-only bridge from TrueNAS but runs no telegraf — only the
native Vector agent. "Local" would mean a whole new collector service that still
remote-writes across the LAN to VM on eq12. Cost of eq12: one LAN hop, one
firewall grant. Benefit: an existing, already-authenticated collector beside the
Grafana that renders the result.

## TrueNAS under IaC

**Scope is the reporting-exporter object only.** Pools, datasets, shares and
permissions stay hand-managed — that is where the irreversible risk lives and
nothing here needs to touch it.

- TrueNAS enters `hosts.yml` in a new `nas` group with
  `ansible_connection: local`. Every task runs on the control node and talks to
  the API over the network; **no SSH path into the appliance is opened.**
- Transport is the official `truenas/api_client`. Ansible's `uri` cannot speak
  WebSocket, so this is unavoidable plumbing rather than a preference.
- **The client is not on PyPI.** It installs only from git and reports its
  version as `0.0.0`, so it cannot be pinned by version — it is pinned by **git
  commit SHA**. Per #137's pinning policy, an unpinned git `HEAD` in the deploy
  path would be worse than anything that policy currently guards against.
- Idempotency: query -> compare -> create/update **only on a real diff**. #86's
  lesson (a module that PUTs full kwargs on any mismatch reports `changed`
  forever and eventually writes something unintended) applies directly.
- `update` takes `(id, payload)`. The payload is partial at the top level, but
  `attributes` is a nested object whose schema marks `exporter_type`,
  `destination_ip`, `destination_port` and `namespace` required with no
  additional properties — so **`attributes` is always sent whole**. Sending only
  changed subfields would drop required ones.
- Credentials are asserted non-empty **by name** before anything runs (#88).
  `no_log` goes on module args, never on the assert.

### The service account

Bootstrapped by hand, once — it is the credential that makes everything after it
IaC, and it is the one deliberate exception to Critical Rule 1. Recorded here so
a DR rebuild can reproduce it.

Group `ansible-ctrl` -> privilege `ansible-ctrl` (roles: `REPORTING_WRITE`, web
shell off) -> user `ansible-ctrl` (primary group `ansible-ctrl`, password
disabled, `smb: false`, shell `nologin`) -> API key `ansible-ctrl`.

The account deliberately **cannot log into the web UI at all**: `READONLY_ADMIN`
is the minimum role for UI access and it is not granted. API only.

Footgun: `privilege.local_groups` and `user.group` take **database entry IDs, not
Unix GIDs** — the docs are explicit that these differ.

Named `ansible-ctrl` rather than for this one task because API keys are per-user
in 26.0, so renaming later means re-minting and re-vaulting the key. Roles are
**not** pre-granted: the role list lives on the privilege and `privilege.update`
edits it in place, leaving the user, group membership and existing key untouched.
Extending later is free; over-granting now is not. Around the third or fourth
role the temptation is to collapse to `FULL_ADMIN` — don't; the explicit list is
the documentation.

Granted roles are mirrored into `truenas_granted_roles` in host_vars with a
per-entry justification, so "what Ansible may do to the NAS" is reviewable in
git rather than living only in the appliance UI.

### Credentials

`ansible/inventory/host_vars/truenas/vault.yml` — following the Proxmox
precedent, where each appliance's API credentials live in its own host vault
rather than `group_vars/all`:

```yaml
vault_truenas_api_username: ansible-ctrl
vault_truenas_api_key: "<shown once at creation, unrecoverable after>"
```

Non-secret settings (destination, prefix, namespace, `update_every`,
`matching_charts`) live in the plaintext sibling `vars.yml`.

## Cardinality budget

Allowlisted to the four chosen families the estimate is **250-400 series**, at a
60s interval. Unfiltered, netdata on this box would push thousands — every app
container, every mountpoint, per-disk latency histogram — into a 5-year TSDB.
The allowlist is therefore a load-bearing control, not tidiness, and it is
enforced **twice**: `matching_charts` at TrueNAS (cheapest, before the data
crosses the LAN) and `namepass` at the listener.

Post-enable check, mirroring what the Grafana-scrape block already prescribes for
itself: `/api/v1/label/__name__/values | grep -c truenas` stays inside budget.

## Metric sources

**Chart inventory measured on the live 26.0 box** (`reporting.netdata_graphs`,
read-only, 2026-08-23) — 40 graphs. This *replaces* the 23.10-era community
mapping as the source of truth for what exists:

| Family | Available in the netdata stream? |
| ------ | -------------------------------- |
| Disk temperature | **Yes** — `disktemp`, per device: `sdb`-`sdf`, `nvme0n1`, `nvme1n1`, each carrying Type (HDD/SSD), Model and Serial |
| Disk I/O | **Yes** — `disk`, same 7 identifiers |
| CPU / temp / load / memory / uptime | **Yes** — `cpu`, `cputemp`, `load`, `memory`, `uptime` |
| ARC / L2ARC | **Yes** — ~20 graphs (`arcsize`, `demand*hitpercentage`, `l2arc*`, ...) |
| Network | **Yes** — `interface`: `ens18` (vmbr1 LAN), `ens19` (vmbr2 NFS) |
| UPS | **Yes** — `upscharge`, `upsload`, `upsvoltage`, `upsruntime`, `upstemperature`, ... (out of scope here) |
| **Pool state / capacity / scrub** | **NO** — no pool or zfs-pool graph exists |
| **NFS op stats** | **NO** — no `nfsd` graph exists |

Two consequences, both corrections to the earlier plan:

1. **Pool health, capacity and scrub state are not in the stream at all.** They
   move wholly to the API poller (#174) via `pool.query`, which makes `POOL_READ`
   a hard requirement of that issue rather than an optional extra.
2. **NFS operation statistics are unavailable** from this source. The practical
   substitute is `ens19` throughput — that interface exists only to carry NFS to
   CT 201 over the host-only bridge, so its traffic *is* the NFS traffic, minus
   per-op latency. Whether real op stats are obtainable is left to #174.

`disktemp` carrying **Type: HDD/SSD** per device is what makes #176's
split-threshold temperature rule implementable without a separate join.

The exporter's `matching_charts` matches raw **netdata chart IDs**, which are a
lower-level namespace than these middleware graph names. Those IDs, and the exact
graphite path shapes, still require the capture step — the read-only enumeration
above narrows what to expect but does not replace it.

### Gauge vs counter

`sda` returns `null` (virtual boot disk, no SMART). The poller emits **explicit
zeros for SMART counters** so that absence stays meaningful (#151), and **skips
nulls for temperature gauges** — a fabricated 0 degrees would also poison every
min/avg panel.

## Verification plan

1. **Schema-first**: build against what `exporter_schemas`/`query` return, not
   the docs. *(done — see evidence base)*
2. **Measurement gate**: capture the real stream to a throwaway sink for one
   window; derive templates and allowlist from captured paths. #172 built its six
   dashboards on measured names; this follows that precedent.
3. **Cardinality assertion** after enabling.
4. **Firewall verified from a BLOCKED source**, not just an allowed one. The rule
   must live in `hook prerouting` at priority -150: :2003 is a docker-published
   port, so an input-hook rule would load cleanly and filter nothing.
5. **Every new guard watched to fail** — `failed_when: false` is banned in this
   chain; it assigns `failed: False` and makes a paired assert vacuous.
6. **Idempotency proven on the SECOND run** against an already-converged
   appliance. The first apply is the one run where even a broken diff looks fine.
7. **Telegraf config change pairs with an explicit restart** — compose ignores
   bind-mount content changes.

## Risks

- **Chart-name drift** between 23.10 and 26.0. Mitigated by the gate; a TrueNAS
  *major* upgrade should re-run it, the same standing rule as re-running the
  restore drill after a PostgreSQL major.
- **`matching_charts` glob dialect** is netdata's simple-pattern syntax. A
  too-broad pattern silently costs cardinality.
- **A new unauthenticated write surface.** Graphite plaintext on :2003 has no
  auth and no encryption; on a flat `192.168.0.0/18` anyone reaching it can
  inject metrics. The `/32` allowlist is the only control. Accepted **because**
  it is scoped to one source — a conscious decision, not a side effect.
- **TrueNAS's LAN IP must be static.** The grant is a source `/32`; `.20` was
  read from ARP and its assignment method is unconfirmed. Prerequisite check.
- **Control-node dependency** must install on Mac and Arch (Critical Rule 2).

## Decomposition

- **#173** — ingest path: inventory, `truenas_reporting` role, exporter, firewall
  grant, telegraf listener, measurement gate. *(this branch)*
- **#174** — API poller sidecar; adds `POOL_READ` (+ optional `DISK_READ`).
- **#175** — NAS dashboard, on measured names.
- **#176** — five alert rules, thresholds from observed series.

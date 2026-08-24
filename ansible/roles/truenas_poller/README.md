# truenas_poller

Polls the TrueNAS JSON-RPC API every 60s and pushes what the Graphite stream
cannot supply into VictoriaMetrics (#174).

## Why it exists

Measured across three controlled `matching_charts` experiments on TrueNAS 26.0
(#173), the netdata Graphite stream carries **no pool health, no scrub state, no
SMART data**, and identifies disks by `serial+lunid` rather than device name.
This role fills exactly those gaps and duplicates nothing the stream already
delivers.

| Metric | Why the stream cannot provide it |
| ------ | -------------------------------- |
| `truenas_disk_temperature_celsius{devname,serial,media}` | stream keys temperature by serial+lunid, and is sparse (minutes) |
| `truenas_disk_info{devname,serial,model,type,media}` | no disk inventory in the stream at all |
| `truenas_pool_healthy`, `truenas_pool_status{status}` | no pool graph exists on 26.0 |
| `truenas_pool_{size,allocated,free}_bytes`, `_fragmentation_percent` | as above |
| `truenas_pool_scrub_{age_seconds,errors,running}` | as above |
| `truenas_poller_up`, `truenas_poller_duration_seconds` | liveness for this delivery path |

## `media` is derived from ROTATION RATE, not from `type`

The API's `type` field says `HDD` for the **QEMU virtual boot disk** (`sda`),
which has no SMART and whose temperature reads as a fabricated `0`. Selecting
spinning disks on `type` would therefore include exactly the disk that must never
appear in a thermal alert.

`rotationrate` is the honest discriminator. Measured 2026-08-23:

| Device | `type` | `rotationrate` | `media` |
| ------ | ------ | -------------- | ------- |
| `sdb`–`sdf` | HDD | 7200 | **hdd** |
| `nvme0n1`, `nvme1n1` | SSD | null | **ssd** |
| `sda` | **HDD** | null | **virtual** |

This label is what let #176 delete its hard-coded serial allowlist, which failed
open the moment a drive was replaced.

## Gauges vs counters

- **Gauges are SKIPPED when unknown, never zeroed.** `sda` returns null for
  temperature; emitting `0` would be a fabricated reading that also poisons every
  min/avg panel.
- **Counters are emitted as explicit zeros** (`truenas_pool_scrub_errors`). A
  counter that only exists once non-zero cannot carry an absence rule (#151) —
  "no series" would be indistinguishable from "poller dead".

## Failure behaviour

On any error the poller **pushes nothing and exits non-zero**. A partial scrape
pushed as if complete would look like a healthy pool with missing disks, which is
worse than no sample. It never pushes `truenas_poller_up 0`: a self-reported
failure would depend on the very path that just failed. Absence is the signal,
and `truenas-poller-absent` owns it.

That rule is deliberately **separate** from `truenas-metrics-absent`. Since the
thermal rules moved to this poller's series, the poller can die while the Graphite
stream keeps flowing — the stream's liveness rule would stay green while the
temperature alerts went quiet. Two delivery paths, two liveness rules.

## Placement and transport

Runs on whichever docker host carries `observability` in its `services` list, and
pushes over **loopback** to VictoriaMetrics' import endpoint. Loopback is
load-bearing: the scoped nftables table already accepts `iif lo`, so this path
needs no allowlist entry and the credential never crosses the flat LAN.

The TrueNAS API key is read through `hostvars['truenas']` so there is exactly one
copy of it, in the appliance's own vault.

## Privileges

Needs `POOL_READ` and `DISK_READ` on the `ansible-ctrl` privilege, on top of the
`REPORTING_WRITE` that #173 established. Grant them in the TrueNAS UI
(Credentials → Groups → Privileges) and record them in `truenas_granted_roles`.

## Deploy

```bash
ansible-playbook playbooks/truenas.yml --check --diff
ansible-playbook playbooks/truenas.yml
journalctl -u truenas-poller.service -n 30   # on the observability host
```

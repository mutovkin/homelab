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
| `truenas_alert_active{klass,level}` (4 pinned klasses + 1 `SMARTUnrecognized` catch-all) | no SMART API survives on 26.0 (removed in 25.10); this is the middleware's own ~90-min smartctl scan, surfaced via `alert.list` |
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

## Drive-health alerts are conclusions, not counters (#183)

TrueNAS 26.0 removed every SMART attribute/test API (measured: `smart.test.results`
and `disk.smart_attributes` return "Method does not exist"). `truenas_alert_active`
therefore reports the middleware's internal ~90-minute smartctl scan — its
*verdict*, not the underlying attributes. Explicitly NOT provided: reallocated /
pending / offline-uncorrectable / UDMA CRC counts, or any sector-level trend
data. Klass→level pairs are pinned in `SMART_ALERT_KLASSES` (from middleware
`release/26.0.0-BETA.2`); extending coverage is a deliberate code change.
Dismissing an alert in the TrueNAS UI zeroes the series — dismissal is the ack
mechanism. The `truenas-smart-degradation` rule over this series is
deliberately NOT in this change: deferred 2026-08-24 to the alerting-file work
batched behind #187 (which rewrites `alerting/*.yaml` wholesale); #183 stays
open until it lands, and the rule must inherit the dismissal semantics by
firing on value > 0.

**Five series: 4 pinned + 1 catch-all.** `SMARTUnrecognized` counts un-dismissed
alerts whose klass starts with `SMART` but is not one of the four. The pinned map
comes from a *BETA* middleware, so a klass rename upstream would otherwise leave
all four series at a healthy `0` while the real alert fired under a name nothing
counted — silent, and shaped exactly like health. The catch-all makes drift and
newly added SMART classes fire instead. Its residual is the prefix heuristic
itself: a klass renamed *away* from `SMART` evades it. Verified 2026-08-24
against `release/26.0.0-BETA.2` across all 70 `alert/source/*.py` (148 alert
classes): exactly four `SMART*` classes exist, and no `SMARTUnrecognized`
collision.

**Two of the four pinned klasses are structurally 0 on this appliance, and their
zeros are not evidence of drive health.** `SMARTSpareBlockCount` and
`SMARTEraseCycleCount` are raised only by `micron_phison_check()`, which returns
early unless the system is enterprise-licensed AND the drive model starts with
`Micron_5210` or `QSP`. Verified 2026-08-24: this appliance runs Seagate Exos
`ST26000NM000C` HDDs, consumer `WD_BLACK SN850X` NVMe and a `QEMU_HARDDISK`
virtual disk, so the model gate alone pins both series to 0 forever regardless
of licence. `SMARTUncorrectedErrors` and `SMARTFailedSelfTest` are the two that
`check_sync()` raises unconditionally per disk — they are the live ones here.

**Two operator actions zero these series, and only one is visible.**
(a) Per-alert **dismissal** — an explicit ack, above. (b) A class-wide
**policy of `NEVER`** in *System → Alert Settings*: `alert.list` filters
everything through `should_show_alert()`, which drops such classes outright, so
the class never appears in the answer at all. That reads as a healthy `0`
forever while the middleware still detects the failure, and it blinds the
catch-all too — a suppressed class cannot even show up as unrecognized. **Do not
set `policy=NEVER` on SMART classes** if these metrics are to mean anything.

**`level` is a pinned label, not a live reading.** It is fixed in code so a
healthy zero and a firing series carry identical label sets. It can therefore
misstate severity two ways: upstream re-levels a klass, or an operator re-levels
it in *Alert Settings* (`get_alert_level()` prefers the operator's value over the
class default). Accepted for label-set identity — the consuming rule treats any
nonzero as actionable regardless of `level`.

The zeros are measured, not fabricated: they are emitted only from a successful
`alert.list` answer, and any failure aborts the whole push (see Failure
behaviour), so a revoked `ALERT_LIST_READ` shows up as poller absence, never as
a healthy zero.

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

Needs `POOL_READ`, `DISK_READ` and `ALERT_LIST_READ` on the `ansible-ctrl`
privilege, on top of the `REPORTING_WRITE` that #173 established. Grant them in
the TrueNAS UI
(Credentials → Groups → Privileges) and record them in `truenas_granted_roles`.

## Deploy

```bash
ansible-playbook playbooks/truenas.yml --check --diff
ansible-playbook playbooks/truenas.yml
journalctl -u truenas-poller.service -n 30   # on the observability host
```

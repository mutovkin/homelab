# UPS Management (Network UPS Tools)

## Overview

| Host                   | Role                | UPS                      | Connection                                   |
|------------------------|---------------------|--------------------------|----------------------------------------------|
| N5 Pro (192.168.30.5)  | NUT server (master) | Goldenmate 1500VA/1000W  | USB (vendorid `075d`, productid `0300`)      |
| EQ12 (192.168.25.5)    | NUT client (slave)  | —                        | Network (TCP 3493)                           |

Both hosts shut down automatically when battery drops to 30%. The server sends an FSD (Full Shut Down) signal to all connected slaves, waits for them to disconnect, then shuts itself down.

## NUT Architecture

NUT is built around three daemons that communicate in a pipeline:

```text
UPS hardware ←USB→ driver (upsdrvctl) → data server (upsd) → monitor (upsmon)
                                                       ↑
                                          remote monitor (upsmon, on client hosts)
```

| Component            | Daemon                       | Role                                                                             |
|----------------------|------------------------------|----------------------------------------------------------------------------------|
| **Driver**           | `usbhid-ups` / `nut-driver`  | Talks to UPS hardware (USB/serial/SNMP). Reads battery, voltage, status.         |
| **Server**           | `upsd` / `nut-server`        | Serves UPS data over TCP 3493. Handles auth and access control.                  |
| **Monitor**          | `upsmon` / `nut-monitor`     | Watches UPS status, triggers shutdown on low battery.                            |
| **Client utilities** | `upsc`, `upscmd`, etc.       | CLI tools for querying status and sending commands (not daemons).                |

A **master** monitor runs on the same host as the driver and server. It owns the shutdown sequence: when battery hits the low threshold, it sends an FSD (Full Shut Down) signal to all connected **slave** monitors, waits for them to shut down, then shuts down the server host itself.

A **slave** monitor runs on a remote host with no direct UPS connection. It connects to `upsd` over the network, receives FSD from the master, and shuts down its own host.

## Shutdown Sequence

```text
Power loss → UPS switches to battery
  → N5 Pro driver detects battery level dropping
  → At 30% charge (override.battery.charge.low): "Low Battery" status triggered
  → N5 Pro upsmon (master) sends FSD to all connected slaves
  → EQ12 upsmon (slave) receives FSD → executes SHUTDOWNCMD → shuts down
  → N5 Pro upsmon (master) waits for slaves to disconnect → executes SHUTDOWNCMD → shuts down
```

## Telemetry

**Decision (#177): NUT is the single source of truth for UPS telemetry. The TrueNAS
netdata UPS graphs are deliberately EXCLUDED from metrics ingest.** This is a choice, not
an oversight — if you are here because you noticed the NAS exports UPS graphs and we do not
ingest them, that is the intent.

### What this UPS actually reports

#194 measured the complete `upsc goldenmate` variable set on the live hardware (Goldenmate
1500VA, driven by `usbhid-ups` with the **`iDowell HID 0.2`** subdriver). It is small:

```text
battery.charge: 100        battery.charge.low: 30     battery.runtime: 60
battery.type: Lion         ups.status: OL             device.model: Smart-Battery
device.mfr: -BMS-          driver.name: usbhid-ups    driver.version.data: iDowell HID 0.2
```

TrueNAS 26.0's `reporting.netdata_graphs` lists eight UPS graphs, whose names promise far
more than this UPS can supply:

| netdata graph | NUT variable it would need | Exists on this hardware? |
|---------------|----------------------------|--------------------------|
| `upscharge`   | `battery.charge`           | **Yes** — 100 %          |
| `upsruntime`  | `battery.runtime`          | **Yes** — seconds        |
| (status)      | `ups.status`               | **Yes** — `OL`/`OB`/`LB` |
| `upsload`     | `ups.load` / `ups.realpower` | No                     |
| `upsvoltage`  | `input.voltage`, `output.voltage` | No                |
| `upscurrent`  | `input.current`, `output.current` | No                |
| `upsfrequency`| `input.frequency`          | No                       |
| `upstemperature` | `ups.temperature`       | No                       |

So **five of the eight graphs are structurally empty on this hardware**. #177 was filed
assuming the UPS surfaces "charge, load, voltage, runtime, temperature"; three of those five
do not exist here, and that premise is corrected by the #194 measurement above. Do not build
a load/voltage/temperature panel — no configuration change can populate it.

### Why the TrueNAS path adds nothing

The UPS is **USB-attached to n5pro**, which runs the driver and `upsd`. The NAS (VM 200 on
n5pro) has no UPS of its own; the only way it could report UPS data is as a *second-hand NUT
client of the same `upsd`* — the identical three numbers, one hop further from the sensor,
stamped with the appliance's identity. It is a second view of one device, not a second
device. eq12 is in the same position (network slave, no local UPS).

### Where the exclusion is enforced

At the appliance-side chart allowlist — `truenas_exporter_matching_charts` in
[`ansible/inventory/host_vars/truenas/vars.yml`](../ansible/inventory/host_vars/truenas/vars.yml),
gate #1 of the two-gate TrueNAS ingest path (see
[`ansible/roles/truenas_reporting/README.md`](../ansible/roles/truenas_reporting/README.md)).

The exclusion is **by construction**: netdata's UPS chart IDs belong to its stock collector
families (`ups*` / `nut*` / `upsd*`), and none of the allowlist's patterns
(`system.* cpu.* disk.* disk_temp* net.* nvme* zfs* truenas*`) matches them — TrueNAS's own
custom `truenas_`-prefixed charts are arcstats, disk_temp, meminfo, pool_usage, disk_stats
and cpu_usage, and UPS is not among them. Nothing UPS-shaped ever leaves the appliance, so
no extra machinery (a `namedrop`, an assert, a narrowed pattern) is warranted or wanted. The
downstream telegraf `namepass` is a backstop only: it passes anything gate #1 emits by
construction, because the graphite template prefixes every measurement `truenas_`.

**Which half of that is measured.** The chart-ID naming above is **inferred** from upstream
netdata's collector naming — it was not read off this appliance's stream, and it cannot be:
the NAS has no UPS service configured, so those charts are not on the wire to inspect. The
**measured** claim is the outcome, and it is the one to trust and to re-run: verified against
the live TSDB on 2026-08-29, the `__name__` label on VictoriaMetrics contains **zero**
UPS/NUT/battery series, and the 59 `truenas_*` families are arcstats, cpu, disk, meminfo,
pool and poller — none UPS.

That split is also why no belt-and-braces `namedrop` is warranted. If the naming inference
were ever wrong — a future TrueNAS release shipping a `truenas_`-prefixed UPS chart — it
would pass both gates and appear as an unexpected series on a dashboard. The failure mode is
**loud, not silent**, which is the same rename-visibility philosophy as telegraf's catch-all
graphite templates: a rename should look wrong on a dashboard, not vanish.

### The gap this leaves — stated, not papered over

**NUT telemetry is not exported to VictoriaMetrics at all today.** `roles/telegraf_agent`
has no NUT input, so `battery.charge`, `battery.runtime` and `ups.status` exist only in
`upsc` output on n5pro. Grafana therefore has no UPS panel and no alert for the UPS leaving
`OL`. Excluding the netdata graphs costs nothing (they would carry the same three numbers,
second-hand) — but it does not close this gap either.

Tracked as **#225**: export the three real variables from n5pro, the host that owns the UPS,
via the native telegraf agent that already runs there.

## Ansible Configuration

The `nut` role in `ansible/roles/nut/` manages everything. Behavior is driven by `nut_role` in host variables.

### Variables

| Variable                    | Location                      | Description                                             |
|-----------------------------|-------------------------------|---------------------------------------------------------|
| `nut_role`                  | `host_vars/<host>/vars.yml`   | `"server"` or `"client"` (empty = disabled)             |
| `nut_server.*`              | `host_vars/n5pro/vars.yml`    | UPS device details, listen address, shutdown command    |
| `nut_client.*`              | `host_vars/eq12/vars.yml`     | Remote server address, UPS name, shutdown command       |
| `vault_nut_master_password` | `group_vars/all/vault.yml`    | Password for master upsmon user                         |
| `vault_nut_slave_password`  | `group_vars/all/vault.yml`    | Password for slave upsmon user                          |

### Deploy

```bash
# Deploy to both hosts
task infra:hosts

# Deploy to a single host
task infra:hosts -- --limit n5pro
task infra:hosts -- --limit eq12
```

### Configuration Files (server — N5 Pro)

| File                                   | Template                  | Purpose                                                    |
|----------------------------------------|---------------------------|------------------------------------------------------------|
| `/etc/nut/nut.conf`                    | `nut.conf.j2`             | `MODE=netserver`                                           |
| `/etc/nut/ups.conf`                    | `ups.conf.j2`             | UPS device definition (driver, vendor, ignorelb, override) |
| `/etc/nut/upsd.conf`                   | `upsd.conf.j2`            | LISTEN address (0.0.0.0:3493)                              |
| `/etc/nut/upsd.users`                  | `upsd.users.j2`           | Master + slave user credentials                            |
| `/etc/nut/upsmon.conf`                 | `upsmon-server.conf.j2`   | MONITOR master + SHUTDOWNCMD                               |
| `/etc/udev/rules.d/90-nut-ups.rules`   | Inline copy               | USB device permissions for nut group                       |

### Configuration Files (client — EQ12)

| File                   | Template                  | Purpose                         |
|------------------------|---------------------------|---------------------------------|
| `/etc/nut/nut.conf`    | `nut.conf.j2`             | `MODE=netclient`                |
| `/etc/nut/upsmon.conf` | `upsmon-client.conf.j2`   | MONITOR slave + SHUTDOWNCMD     |

### Services (server)

- `nut-driver@goldenmate.service` — UPS hardware driver
- `nut-server.service` — upsd (serves UPS data over network)
- `nut-monitor.service` (or `nut-client.service` on older Debian) — upsmon (monitoring + shutdown)

### Services (client)

- `nut-monitor.service` — upsmon (remote monitoring + shutdown)

## Operations

### Check UPS Status

```bash
# From N5 Pro (server)
upsc goldenmate

# From EQ12 (client) or any network host
upsc goldenmate@192.168.30.5
```

Key fields to check:

- `ups.status: OL` — Online (on AC power)
- `ups.status: OB` — On Battery
- `ups.status: LB` — Low Battery
- `battery.charge` — Current charge percentage
- `battery.charge.low: 30` — Confirms override is active

### View Logs

```bash
# On N5 Pro
journalctl -u nut-driver@goldenmate -u nut-server -u nut-monitor

# On EQ12
journalctl -u nut-monitor
```

### Test Shutdown (FSD Simulation)

**Warning: This will shut down all connected hosts. Only run during a maintenance window.**

```bash
# Trigger FSD from the server
upsmon -c fsd
```

### Debug Driver Issues

If the driver fails to connect to the UPS:

```bash
# Stop the background service
systemctl stop nut-driver@goldenmate

# Run driver in foreground with debug output
/lib/nut/usbhid-ups -a goldenmate -DD
```

## Adding a New Host

To add another host (e.g., a new mini PC) as a NUT client:

1. Add `nut_role: "client"` and `nut_client` block to the host's `vars.yml`:

    ```yaml
    nut_role: "client"
    nut_client:
      monitor_ups_name: "goldenmate"
      monitor_host: "192.168.30.5"
      shutdowncmd: "/sbin/shutdown -h +0"
      powerdownflag: "/etc/killpower"
    ```

2. Ensure the host is in the `proxmox_hosts` inventory group.
3. Run `task infra:hosts -- --limit <hostname>`.

The slave credentials in the shared vault are already available to all hosts in the group.

## Troubleshooting

| Symptom                    | Check                                                                                    |
|----------------------------|------------------------------------------------------------------------------------------|
| Driver not connecting      | USB cable connected? `lsusb` shows device? Udev rule applied?                            |
| Driver "Data stale"        | `systemctl restart nut-driver@goldenmate`                                                |
| Client can't reach server  | `ss -tlnp \| grep 3493` on N5 Pro — should show LISTEN                                   |
| Permission denied on USB   | Check `/etc/udev/rules.d/90-nut-ups.rules` and `udevadm trigger`                         |
| Config not updating        | Ansible templates overwrite manual edits — always edit vars, not remote files            |

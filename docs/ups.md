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

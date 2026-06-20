---
title: "macOS Local Network privacy silently blocks Ansible's Python from reaching LAN Proxmox APIs"
date: 2026-06-20
category: developer-experience
module: control-machine
problem_type: developer_experience
component: development_workflow
severity: medium
applies_when:
  - "running Ansible or Python network clients from macOS against LAN hosts"
  - "an Ansible task uses delegate_to localhost to reach a 192.168.x.x API"
  - "after a macOS upgrade a previously working deploy fails with No route to host"
  - "setting up a new macOS control machine for this repo"
  - "curl/ping/nc succeed but Python/Ansible report [Errno 65] No route to host to LAN only"
related_components:
  - ansible
  - proxmox
tags:
  - macos
  - ansible
  - networking
  - proxmox
  - local-network-privacy
  - errno-65
  - control-machine
  - uv
---

# macOS Local Network privacy silently blocks Ansible's Python from reaching LAN Proxmox APIs

## Context

This repo is driven from a Mac control machine (Homebrew + `uv`-managed
`ansible-core`). When running `task deploy:full`, the
`community.proxmox.proxmox_kvm` "Provision virtual machines" task runs with
`delegate_to: localhost`, so the laptop's Python interpreter makes HTTPS calls
directly to the Proxmox APIs at `https://192.168.25.5:8006` and
`https://192.168.30.5:8006`. The task failed with:

```
HTTPSConnectionPool(host='192.168.25.5', port=8006): Max retries exceeded ...
Failed to establish a new connection: [Errno 65] No route to host
```

The confusing part: from the **same Mac**, `ping`, `nc -zv <host> 8006`, and
`curl -k https://<host>:8006/api2/json/version` (returning the expected HTTP 401)
all succeeded. Only Python/Ansible could not connect — and only to the LAN, not
the internet.

## Guidance

- **Diagnostic signature:** `curl`, `ping`, and `nc` to a LAN host all succeed,
  but Python/Ansible get `[Errno 65] No route to host` — and only to LAN
  (`192.168.x.x`), while internet destinations work from Python. That asymmetry
  (LAN fails, WAN works, same machine, non-Python tools work) is the giveaway.
- **Root cause:** macOS Local Network privacy protection. The Python interpreter
  Ansible uses had not been granted Local Network access, so macOS silently
  blocked its LAN connections while allowing `curl`/`ping` (already permitted) and
  internet traffic.
- **Fix:** Grant Local Network permission to the Python interpreter Ansible runs,
  in **System Settings → Privacy & Security → Local Network**.
- **Workaround:** Apps already granted Local Network access (e.g. iTerm) let child
  processes inherit it — running the deploy from a granted terminal succeeds
  without changing the interpreter's own permission.
- **Aside (not the cause):** the `uv` ansible-core env was also missing `certifi`,
  fixed with `uv pip install certifi --python ~/.local/share/uv/tools/ansible-core/bin/python`.
  This was a real but separate dependency gap; it did **not** cause errno 65. Don't
  let fixing it convince you the network problem is solved.

## Why This Matters

The failure is silent at the OS layer — macOS blocks the connection without a
permission prompt in this path, so the error masquerades as a routing/network
problem. Operators can burn significant time checking firewalls, routes, DNS, and
Proxmox itself. The `errno-65-from-Python-but-curl-works` signature is the fastest
way to recognize it. Apple tightened Local Network privacy enforcement in recent
macOS versions, so this surfaces on upgrades and fresh setups even when nothing in
the repo changed. The Arch/Omarchy control machine is unaffected — this is
macOS-specific.

## When to Apply

- Running Ansible (or any Python network client) from macOS against LAN hosts,
  especially modules using `delegate_to: localhost` that hit `192.168.x.x` APIs.
- After a macOS upgrade, when a previously working deploy starts failing with
  `No route to host` to LAN only.
- Setting up a new Mac control machine for this repo.

## Examples

Ansible/Python failure (LAN):
```
HTTPSConnectionPool(host='192.168.25.5', port=8006): Max retries exceeded ... [Errno 65] No route to host
```

Same host via curl (succeeds, 401 expected):
```bash
curl -k https://192.168.25.5:8006/api2/json/version   # -> HTTP 401, connection OK
```

Python socket probe — LAN blocked vs internet allowed:
```bash
python3 -c "import socket; s=socket.socket(); s.settimeout(5); print(s.connect_ex(('192.168.25.5',8006)))"  # -> 65 (blocked, LAN)
python3 -c "import socket; s=socket.socket(); s.settimeout(5); print(s.connect_ex(('8.8.8.8',53)))"          # -> 0 (allowed, internet)
```

## Related

- This learning has a one-line counterpart in `CLAUDE.md` (Gotchas →
  "macOS Local Network privacy"). Keep the two consistent.

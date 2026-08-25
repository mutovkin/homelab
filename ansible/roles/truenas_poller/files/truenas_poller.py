#!/usr/bin/env python3
"""Poll the TrueNAS JSON-RPC API and push Prometheus metrics to VictoriaMetrics.

WHY THIS EXISTS (#174). The netdata Graphite stream (#173) cannot supply what a
NAS dashboard most needs. Measured across three controlled matching_charts
experiments on TrueNAS 26.0, that stream carries no pool health, no scrub state,
no SMART data, and identifies disks by serial+lunid rather than device name.
This poller fills exactly those gaps and deliberately duplicates nothing the
stream already delivers well.

WHY PUSH RATHER THAN SCRAPE. Pushing to VictoriaMetrics' import endpoint needs no
listening socket, no firewall grant and no bridge-address binding. It is the same
transport Home Assistant uses (#133) and the native Vector agents use (#160), so
it is the established shape here rather than a new one.

Absence is made unambiguous the way #133 did it: truenas_poller_up is exported on
every successful cycle, so silence is a fact rather than a question.
"""

import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from truenas_api_client import Client

API_URL = os.environ["TRUENAS_API_URL"]
API_KEY = os.environ["TRUENAS_API_KEY"]
VM_URL = os.environ["VM_IMPORT_URL"]
VM_USER = os.environ["VM_AUTH_USERNAME"]
VM_PASS = os.environ["VM_AUTH_PASSWORD"]
HOST = os.environ.get("TRUENAS_HOST_LABEL", "truenas")

# Middleware drive-health alert klasses -> alert LEVEL, pinned from
# truenas/middleware release/26.0.0-BETA.2 alert/source/smart.py (the klass
# string is the class name minus "AlertClass"). The level label is pinned
# HERE rather than read from the alert instance so the healthy zero-valued
# series and a firing series carry IDENTICAL label sets — Grafana alert
# dedup and the #151 absence contract both depend on that. Extending this
# map is a code change by design: a klass not listed here has no healthy
# zero, and a series that only exists once non-zero cannot carry a rule.
SMART_ALERT_KLASSES = {
    "SMARTUncorrectedErrors": "WARNING",
    "SMARTFailedSelfTest": "WARNING",
    "SMARTSpareBlockCount": "CRITICAL",
    "SMARTEraseCycleCount": "WARNING",
}


def esc(v):
    """Escape a Prometheus label value."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(**kw):
    return ",".join(f'{k}="{esc(v)}"' for k, v in sorted(kw.items()) if v is not None)


def media_of(disk):
    """Classify a disk.

    `type` ALONE IS NOT ENOUGH, and this is the trap that made this function
    necessary: the QEMU virtual boot disk (sda) reports type="HDD" with
    rotationrate=None. Selecting spinning disks on type would therefore include a
    disk that has no SMART at all and whose temperature reads as a fabricated 0 —
    exactly the series #176 must never alert on. rotationrate is the honest
    discriminator: 7200 on the five real Exos drives, None on both NVMe and on
    the virtual disk.
    """
    if disk.get("rotationrate"):
        return "hdd"
    if (disk.get("type") or "").upper() == "SSD":
        return "ssd"
    return "virtual"


def collect(client):
    out = []
    add = out.append

    disks = client.call("disk.query")
    temps = client.call("disk.temperatures")

    add("# HELP truenas_disk_info Disk inventory; value is always 1.")
    add("# TYPE truenas_disk_info gauge")
    add("# HELP truenas_disk_size_bytes Disk capacity in bytes.")
    add("# TYPE truenas_disk_size_bytes gauge")
    add("# HELP truenas_disk_temperature_celsius Disk temperature by DEVICE NAME.")
    add("# TYPE truenas_disk_temperature_celsius gauge")

    for d in disks:
        dev = d.get("devname")
        if not dev:
            continue
        media = media_of(d)
        info = labels(host=HOST, devname=dev, serial=d.get("serial"),
                      model=d.get("model"), type=d.get("type"), media=media)
        add(f"truenas_disk_info{{{info}}} 1")
        if d.get("size") is not None:
            add(f'truenas_disk_size_bytes{{{labels(host=HOST, devname=dev)}}} {d["size"]}')

        t = temps.get(dev)
        # A GAUGE IS SKIPPED WHEN UNKNOWN, NEVER ZEROED. sda returns null here
        # (no SMART), and emitting 0C would be a fabricated reading that also
        # poisons every min/avg panel. Contrast with the counters below, which
        # ARE emitted as explicit zeros so that their absence stays meaningful.
        if t is not None:
            add(f"truenas_disk_temperature_celsius{{{labels(host=HOST, devname=dev, serial=d.get('serial'), media=media)}}} {float(t)}")

    pools = client.call("pool.query")

    add("# HELP truenas_pool_healthy 1 if the pool reports healthy, else 0.")
    add("# TYPE truenas_pool_healthy gauge")
    add("# HELP truenas_pool_size_bytes Pool total size in bytes.")
    add("# TYPE truenas_pool_size_bytes gauge")
    add("# HELP truenas_pool_scrub_age_seconds Seconds since the last scrub finished.")
    add("# TYPE truenas_pool_scrub_age_seconds gauge")

    now = datetime.now(timezone.utc)
    for p in pools:
        name = p.get("name")
        pl = labels(host=HOST, pool=name)
        add(f"truenas_pool_healthy{{{pl}}} {1 if p.get('healthy') else 0}")
        # status carried as a LABEL on a constant series, so a dashboard can show
        # ONLINE/DEGRADED as text and an alert can match on it, without inventing
        # a numeric encoding that both ends have to agree about.
        add(f'truenas_pool_status{{{labels(host=HOST, pool=name, status=p.get("status"))}}} 1')
        for field, metric in (("size", "truenas_pool_size_bytes"),
                              ("allocated", "truenas_pool_allocated_bytes"),
                              ("free", "truenas_pool_free_bytes")):
            if p.get(field) is not None:
                add(f"{metric}{{{pl}}} {p[field]}")
        try:
            add(f'truenas_pool_fragmentation_percent{{{pl}}} {float(p.get("fragmentation"))}')
        except (TypeError, ValueError):
            pass

        scan = p.get("scan") or {}
        # Counters: emitted as explicit ZEROS on a healthy pool. A counter that
        # only exists once non-zero cannot carry an absence rule (#151) — "no
        # series" would be indistinguishable from "poller dead".
        add(f'truenas_pool_scrub_errors{{{pl}}} {int(scan.get("errors") or 0)}')
        add(f'truenas_pool_scrub_running{{{pl}}} {1 if scan.get("state") == "SCANNING" else 0}')
        end = scan.get("end_time")
        if isinstance(end, datetime):
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            add(f"truenas_pool_scrub_age_seconds{{{pl}}} {int((now - end).total_seconds())}")

    # ── Middleware drive-health alerts (#183, re-scoped) ────────────────────
    # TrueNAS 26.0 removed every SMART API surface (measured 2026-08-23:
    # smart.test.results / disk.smart_attributes -> "Method does not exist";
    # SMART left the middleware in 25.10). The four raw counters #183 wanted
    # are unobtainable without root-exec on the appliance, which the operator
    # declined. alert.list (ALERT_LIST_READ) is the lawful remainder: the
    # middleware's OWN ~90-minute smartctl scan, surfaced as conclusions.
    # This is NOT sector-level trend data and must never be presented as such.
    alerts = client.call("alert.list")

    add("# HELP truenas_alert_active Count of un-dismissed TrueNAS middleware alerts of this klass. Exists as 0 while healthy.")
    add("# TYPE truenas_alert_active gauge")
    # EMITTED AS EXPLICIT ZEROS — THE OPPOSITE OF THE TEMPERATURE GAUGE ABOVE,
    # AND BOTH RULES ARE LOAD-BEARING. Temperature is a READING from a device:
    # when the device cannot answer (sda has no SMART) the honest value is NO
    # value, and a zero would fabricate a healthy reading. This series is a
    # COUNT over a successful alert.list answer: the appliance responded, and
    # "zero alerts of this klass" is a fact it just asserted — the zero is
    # measured, not fabricated. DO NOT UNIFY THESE TWO BEHAVIOURS. The guard
    # against fabrication here is the call itself: if alert.list raises (role
    # revoked, API down), collect() aborts and the cycle pushes NOTHING, so a
    # failure can never be dressed as a healthy zero — the poller goes absent
    # and truenas-poller-absent owns that signal.
    for klass, level in SMART_ALERT_KLASSES.items():
        n = sum(1 for a in alerts
                if a.get("klass") == klass and not a.get("dismissed"))
        add(f'truenas_alert_active{{{labels(host=HOST, klass=klass, level=level)}}} {n}')

    return out


def push(body):
    req = urllib.request.Request(
        VM_URL, data=("\n".join(body) + "\n").encode(), method="POST")
    auth = __import__("base64").b64encode(f"{VM_USER}:{VM_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status >= 300:
            raise RuntimeError(f"VictoriaMetrics returned HTTP {r.status}")


def main():
    started = time.time()
    try:
        with Client(API_URL, verify_ssl=False) as client:
            if not client.call("auth.login_with_api_key", API_KEY):
                raise RuntimeError("TrueNAS rejected the API key")
            body = collect(client)
    except Exception as exc:
        # Fail LOUDLY and push nothing. A partial scrape pushed as if complete
        # would look like a healthy pool with missing disks, which is worse than
        # no sample: truenas_poller_up simply stops, and the liveness rule owns
        # that. Never push truenas_poller_up=0 here — a self-reported failure
        # depends on the very path that just failed.
        print(f"truenas-poller: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    body.append("# HELP truenas_poller_up 1 when a full poll cycle succeeded.")
    body.append("# TYPE truenas_poller_up gauge")
    body.append(f'truenas_poller_up{{{labels(host=HOST)}}} 1')
    body.append(f'truenas_poller_duration_seconds{{{labels(host=HOST)}}} {round(time.time() - started, 3)}')

    try:
        push(body)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"truenas-poller: push failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Reconcile the Home Assistant entity set against what VictoriaMetrics stored.

Issue #171 item 2. Answers "which HA entities are dead?" WITHOUT trusting HA's own
metadata, because HA's `last_changed` is rewritten at restore: measured 2026-08-24,
0 of 574 not-yet-seen entities read older than 7 days, including one device dead
since 2023-08-20. VictoriaMetrics' sample timestamps are the only honest source.

Read-only. Issues exactly three kinds of request:
  * HA   GET  /api/states
  * VM   GET  /api/v1/series          (match[]={db="ha"} ...)
  * VM   POST /api/v1/query, /api/v1/query_range   (read-only query APIs)
It mutates nothing, anywhere.

Credentials are read from ansible-vault at runtime and are never printed, never
written to disk, and never embedded in an exception message.

Every conclusion this script prints is bounded by the window it prints in its
header: an entity absent from VictoriaMetrics is evidence of death only if it was
expected to report inside that window. See docs/solutions/conventions/
instant-query-cannot-prove-a-series-is-live.md.

Reproducibility, stated precisely because the naive claim is false. Everything
derived from VictoriaMetrics is a pure function of (--start, --end) and repeats
byte-for-byte. HA's /api/states is NOT: it is a live source, and an entity that
changes state now moves its `last_changed` past a pinned `end`, which shrinks the
G3 positive-control set (measured 2026-08-24: ~11 entities/min, the only two lines
that moved across four pinned re-runs). So --ha-states-json pins that side too:
the first run captures the payload, later runs replay it, and only then is stdout
byte-identical end to end. Snapshots carry personal home-state data -- keep them
out of the repo (the script refuses a path inside it).

Usage:
    export ANSIBLE_VAULT_PASSWORD_FILE=/path/to/.vault_password
    scripts/reconcile-ha-entities.py                     # widest available window
    scripts/reconcile-ha-entities.py --start 1787443200 --end 1787541146
    scripts/reconcile-ha-entities.py --ha-states-json /tmp/ha-states.json

Exit codes: 0 = all guards passed, 2 = a guard failed (do not publish the report).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants pinned by issue #171 and its comments (measured, not guessed).
# ---------------------------------------------------------------------------

DEFAULT_VM_URL = "http://192.168.25.15:8428"
DEFAULT_HA_URL = "http://192.168.25.10:8123"

VAULT_ALL = ("ansible", "inventory", "group_vars", "all", "vault.yml")
VAULT_EQ12_DOCKER = ("ansible", "inventory", "host_vars", "eq12_docker", "vault.yml")

# HA never writes these states: event_to_json() returns None for them (#171).
ABSENT_BY_DESIGN_STATES = {"unavailable", "unknown"}

# Excluded by design in HA's influxdb: filter. `button`/`scene` exclusions were
# REMOVED in #171 item 1 -- do not add them back here.
EXCLUDED_DOMAINS = {"update"}

# States that are not float() but that HA's state_as_number() maps to a number,
# so an entity in one of these can still produce a numeric field.
BINARY_STATE_VOCABULARY = {
    "on",
    "off",
    "true",
    "false",
    "open",
    "closed",
    "home",
    "not_home",
    "locked",
    "unlocked",
    "above_horizon",
    "below_horizon",
}

# The nine devices #133 derived from the `last_seen` series. This script is the
# second, independent mechanism; if the two disagree, the disagreement is the
# finding (issue #171). Silence figures are as published, dated 2026-08-23.
NINE_DEVICES = (
    ("stairs_sensor", 1098, "2023-08-20"),
    ("office_motion_sensor", 285, "2025-11-10"),
    ("anthonys_bathroom_lights", 285, "2025-11-09"),
    ("office_exterior_door", 274, "2025-11-20"),
    ("front_door_motion_sensor", 172, "2026-03-03"),
    ("backyard_motion_sensor", 155, "2026-03-19"),
    ("christmas_tree_lights", 148, "2026-03-26"),
    ("anthony_s_bathroom_motion_sensor", 120, "2026-04-24"),
    ("backyard_patio_door", 31, "2026-07-21"),
)

# G1 bounds: 1213 entities measured in #133 and again in #171 comment 3.
G1_MIN, G1_MAX = 1100, 1400
# G2 bound: 210 distinct (domain, entity_id) pairs at a 6.8h window; a wider
# window can only grow it.
G2_MIN = 150
# G3 bounds.
G3_MIN_CONTROL = 10
G3_MIN_HIT_RATE = 0.90
G3_WINDOW_S = 1800
G3_WIDENED_WINDOW_S = 7200

HTTP_TIMEOUT = 120


class Failure(RuntimeError):
    """Fatal, operator-readable error. Never carries a credential."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def vault_view(repo_root: Path, rel_parts: tuple[str, ...]) -> str:
    """Decrypt one vault file. Returns plaintext; NEVER log the return value."""
    path = repo_root.joinpath(*rel_parts)
    if not path.is_file():
        raise Failure(f"vault file not found: {path}")

    cmd = ["ansible-vault", "view", str(path)]
    env = os.environ.copy()
    if not env.get("ANSIBLE_VAULT_PASSWORD_FILE"):
        fallback = repo_root / ".vault_password"
        if not fallback.is_file():
            raise Failure(
                "no vault password available: set ANSIBLE_VAULT_PASSWORD_FILE, or "
                f"provide {fallback} (a git worktree does not carry it -- the env "
                "var is the normal path)"
            )
        cmd += ["--vault-password-file", str(fallback)]

    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_root), env=env, capture_output=True, text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise Failure("ansible-vault not on PATH") from exc
    if proc.returncode != 0:
        # stderr of ansible-vault carries no plaintext, only decrypt diagnostics.
        raise Failure(
            f"ansible-vault view {path.name} failed rc={proc.returncode}: "
            f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}"
        )
    return proc.stdout


def vault_key(plaintext: str, key: str, where: str) -> str:
    """Pull one scalar out of decrypted vault YAML. Value is never echoed."""
    prefix = f"{key}:"
    for line in plaintext.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if not value:
                raise Failure(f"{key} is empty in {where}")
            return value
    raise Failure(f"{key} not found in {where}")


# ---------------------------------------------------------------------------
# HTTP helpers. Exceptions carry the URL PATH and the status only -- never the
# query string, never a header, so no Authorization value can leak into a
# traceback or a CI log.
# ---------------------------------------------------------------------------


def _request(req: urllib.request.Request, what: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raise Failure(f"{what}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise Failure(f"{what}: unreachable ({exc.reason})") from None
    except TimeoutError:
        raise Failure(f"{what}: timed out after {HTTP_TIMEOUT}s") from None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise Failure(f"{what}: response was not JSON ({len(payload)} bytes)") from None


def ha_get(ha_url: str, path: str, token: str) -> list | dict:
    req = urllib.request.Request(
        ha_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    return _request(req, f"HA GET {path}")


def vm_post(vm_url: str, path: str, params: list[tuple[str, str]], auth: str) -> dict:
    """POST to a VictoriaMetrics READ endpoint (query / query_range / series)."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        vm_url.rstrip("/") + path,
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    doc = _request(req, f"VM POST {path}")
    if doc.get("status") != "success":
        raise Failure(f"VM POST {path}: status={doc.get('status')!r}")
    return doc


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_ha_ts(value: str) -> float | None:
    """HA emits ISO-8601 with a +00:00 offset; tolerate a trailing Z too."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def numeric_capable(state: str) -> bool:
    """Could this state ever yield a numeric field in the influxdb push?

    float() first, then HA's state_as_number() binary vocabulary. A state that
    passes neither can only be stored as a string field -- so its entity may be
    STRUCTURALLY INVISIBLE to a numeric-only reconciliation even while alive.
    """
    try:
        float(state)
        return True
    except (TypeError, ValueError):
        pass
    return state.lower() in BINARY_STATE_VOCABULARY


def fmt_silence(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def detect_start(vm_url: str, auth: str, end: int) -> int:
    """Widest window with data: the first 30-min bucket of {db="ha"} that has a
    value, minus one bucket for alignment slop. Fails loudly on an empty range --
    an auto-detected start must never silently become "8 days ago"."""
    doc = vm_post(
        vm_url,
        "/api/v1/query_range",
        [
            ("query", 'count({db="ha"})'),
            ("start", str(end - 8 * 86400)),
            ("end", str(end)),
            ("step", "1800"),
        ],
        auth,
    )
    result = doc["data"]["result"]
    if not result:
        raise Failure(
            'auto-detect start: count({db="ha"}) returned no series over the last '
            "8 days -- is the HA push alive?"
        )
    stamps = sorted(int(point[0]) for series in result for point in series["values"])
    if not stamps:
        raise Failure('auto-detect start: count({db="ha"}) has no populated bucket')
    return stamps[0] - 1800


def fetch_ha_states(ha_url: str, token: str) -> list[dict]:
    states = ha_get(ha_url, "/api/states", token)
    if not isinstance(states, list):
        raise Failure("HA /api/states did not return a list")
    return states


def load_or_capture_ha_states(
    ha_url: str, token: str, snapshot: Path | None, repo_root: Path
) -> list[dict]:
    """HA states, optionally pinned to a snapshot file so a re-run is reproducible.

    Provenance goes to STDERR, never stdout: a "captured" vs "replayed" line on
    stdout would make the capture run and the replay run differ, which is exactly
    the property the snapshot exists to establish.
    """
    if snapshot is None:
        return fetch_ha_states(ha_url, token)

    snapshot = snapshot.expanduser().resolve()
    # /api/states is a full readout of a home: occupancy, device names, presence.
    # It must never land in the repo, where a stray `git add -A` would publish it.
    if snapshot == repo_root or repo_root in snapshot.parents:
        raise Failure(
            f"refusing to use an HA snapshot inside the repo ({snapshot}): "
            "/api/states carries personal home-state data. Put it in a scratch "
            "directory outside the working tree."
        )

    if snapshot.is_file():
        try:
            states = json.loads(snapshot.read_text())
        except json.JSONDecodeError as exc:
            raise Failure(f"HA snapshot {snapshot} is not valid JSON: {exc.msg}") from None
        if not isinstance(states, list) or not states:
            raise Failure(f"HA snapshot {snapshot} does not hold a non-empty list")
        print(f"HA states REPLAYED from snapshot {snapshot} ({len(states)} entities)",
              file=sys.stderr)
        return states

    states = fetch_ha_states(ha_url, token)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600: see the personal-data note above.
    fd = os.open(str(snapshot), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(states, handle)
    print(f"HA states CAPTURED to snapshot {snapshot} ({len(states)} entities)",
          file=sys.stderr)
    return states


def fetch_vm_series(
    vm_url: str, auth: str, start: int, end: int, match: str
) -> list[dict]:
    doc = vm_post(
        vm_url,
        "/api/v1/series",
        [("match[]", match), ("start", str(start)), ("end", str(end))],
        auth,
    )
    return doc.get("data", []) or []


def fetch_vm_last_samples(
    vm_url: str, auth: str, start: int, end: int
) -> dict[str, float]:
    """Raw last-sample unix timestamp per (domain, entity_id) over the window.

    tlast_over_time is verified against /api/v1/export before every run (see the
    PR's verification section); it returns the SAMPLE timestamp, not the query
    time -- which is the whole point, per instant-query-cannot-prove-a-series-is-live.
    """
    window = end - start
    doc = vm_post(
        vm_url,
        "/api/v1/query",
        [
            (
                "query",
                f'max(tlast_over_time({{db="ha"}}[{window}s])) by (domain, entity_id)',
            ),
            ("time", str(end)),
        ],
        auth,
    )
    out: dict[str, float] = {}
    for item in doc["data"]["result"]:
        metric = item.get("metric", {})
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if not domain or not entity:
            continue
        key = f"{domain}.{entity}"
        ts = float(item["value"][1])
        out[key] = max(out.get(key, 0.0), ts)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile HA's entity set against VictoriaMetrics (issue #171)."
    )
    parser.add_argument("--start", type=int, default=None,
                        help="window start, epoch seconds (default: auto-detect the "
                             "earliest {db=\"ha\"} bucket)")
    parser.add_argument("--end", type=int, default=None,
                        help="window end, epoch seconds (default: now)")
    parser.add_argument("--vm-url", default=DEFAULT_VM_URL)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--ha-states-json", default=None,
                        help="pin the HA side: capture /api/states here on first use, "
                             "replay it afterwards. Required to make stdout "
                             "byte-identical across runs, because HA is a live source. "
                             "Must live OUTSIDE the repo -- it holds home-state data.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()

    # -- credentials ---------------------------------------------------------
    all_vault = vault_view(repo_root, VAULT_ALL)
    vm_user = vault_key(all_vault, "vault_vm_auth_username", "group_vars/all/vault.yml")
    vm_pass = vault_key(all_vault, "vault_vm_auth_password", "group_vars/all/vault.yml")
    eq12_vault = vault_view(repo_root, VAULT_EQ12_DOCKER)
    ha_token = vault_key(
        eq12_vault, "vault_ha_long_lived_token", "host_vars/eq12_docker/vault.yml"
    )
    del all_vault, eq12_vault
    vm_auth = base64.b64encode(f"{vm_user}:{vm_pass}".encode()).decode()
    del vm_pass

    # -- window --------------------------------------------------------------
    # The ONLY wallclock read in the script; everything downstream is a function
    # of (start, end), which is what makes a pinned re-run reproducible.
    end = args.end if args.end is not None else int(time.time())
    start = args.start if args.start is not None else detect_start(args.vm_url, vm_auth, end)
    if start >= end:
        raise Failure(f"start ({start}) must be before end ({end})")
    window = end - start

    out: list[str] = []
    def emit(line: str = "") -> None:
        out.append(line)

    emit("=" * 78)
    emit("HA <-> VictoriaMetrics entity-set reconciliation (issue #171 item 2)")
    emit("=" * 78)
    emit(f"WINDOW   : {iso(start)} .. {iso(end)}  ({window / 3600:.1f}h)")
    emit(f"           --start {start} --end {end}   (pin these to reproduce)")
    emit(f"HA       : {args.ha_url}/api/states")
    emit(f"VM       : {args.vm_url}  (series + tlast_over_time, read-only)")
    emit()

    # -- HA side -------------------------------------------------------------
    states = load_or_capture_ha_states(
        args.ha_url,
        ha_token,
        Path(args.ha_states_json) if args.ha_states_json else None,
        repo_root,
    )
    del ha_token

    ha: dict[str, dict] = {}
    for item in states:
        full_id = item.get("entity_id", "")
        if "." not in full_id:
            continue
        ha[full_id] = {
            "domain": full_id.split(".", 1)[0],
            "state": item.get("state", ""),
            "last_changed": parse_ha_ts(item.get("last_changed", "")),
        }

    absent_by_design, excluded_update, writable = [], [], []
    overlap_update_absent = 0
    for full_id, rec in ha.items():
        if rec["domain"] in EXCLUDED_DOMAINS:
            excluded_update.append(full_id)
            if rec["state"] in ABSENT_BY_DESIGN_STATES:
                overlap_update_absent += 1
        elif rec["state"] in ABSENT_BY_DESIGN_STATES:
            absent_by_design.append(full_id)
        else:
            writable.append(full_id)
    absent_by_design.sort()
    excluded_update.sort()
    writable.sort()

    # -- VM side -------------------------------------------------------------
    series = fetch_vm_series(args.vm_url, vm_auth, start, end, '{db="ha"}')
    vm_seen: set[str] = set()
    unjoinable = []
    metric_names: set[str] = set()
    for metric in series:
        name = metric.get("__name__", "")
        if name:
            metric_names.add(name)
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if domain and entity:
            vm_seen.add(f"{domain}.{entity}")
        else:
            unjoinable.append(name or "<unnamed>")

    last_sample = fetch_vm_last_samples(args.vm_url, vm_auth, start, end)
    update_domain_series = fetch_vm_series(
        args.vm_url, vm_auth, start, end, '{db="ha",domain="update"}'
    )
    update_prefixed_names = sorted(n for n in metric_names if n.startswith("update."))

    # -- join ----------------------------------------------------------------
    writable_set = set(writable)
    seen_writable = sorted(writable_set & vm_seen)
    never_seen = sorted(writable_set - vm_seen)
    never_seen_numeric = [e for e in never_seen if numeric_capable(ha[e]["state"])]
    never_seen_string = [e for e in never_seen if not numeric_capable(ha[e]["state"])]
    vm_only = sorted(vm_seen - set(ha))
    # VM has seen these, but HA now reports them unavailable/unknown -- the
    # freshest-dead / flapping shape. Without a name they vanish into the gap
    # between "VM distinct pairs" and "writable AND VM-seen".
    seen_now_absent = sorted(vm_seen & set(absent_by_design))
    seen_now_update = sorted(vm_seen & set(excluded_update))

    silence = {e: end - last_sample[e] for e in seen_writable if e in last_sample}
    seen_no_ts = sorted(e for e in seen_writable if e not in last_sample)

    # -- counts --------------------------------------------------------------
    emit("-- COUNTS " + "-" * 67)
    emit(f"HA entities total                     {len(ha):5d}")
    emit(f"  excluded by design (domain=update)  {len(excluded_update):5d}"
         f"   (of which unavailable/unknown: {overlap_update_absent})")
    emit(f"  absent by design (unavail/unknown)  {len(absent_by_design):5d}"
         "   HA never writes these: event_to_json -> None")
    emit(f"  writable                            {len(writable):5d}")
    emit(f"VM series over window                 {len(series):5d}"
         f"   ({len(metric_names)} distinct metric names)")
    emit(f"VM distinct (domain, entity_id)       {len(vm_seen):5d}")
    emit(f"  series lacking domain or entity_id  {len(unjoinable):5d}   (cannot join)")
    for name in sorted(set(unjoinable))[:10]:
        emit(f"      {name}")
    emit(f"writable AND VM-seen                  {len(seen_writable):5d}")
    emit(f"VM-seen, now unavailable/unknown       {len(seen_now_absent):5d}"
         "   seen in-window, HA reports it gone now")
    emit(f"VM-seen, domain=update                 {len(seen_now_update):5d}"
         "   expected 0 (G4 subtraction)")
    emit(f"writable NEVER seen                   {len(never_seen):5d}")
    emit(f"  numeric-capable state (candidates)  {len(never_seen_numeric):5d}")
    emit(f"  string-only state (INCONCLUSIVE)    {len(never_seen_string):5d}")
    emit(f"VM-seen with no HA entity (vm_only)   {len(vm_only):5d}")
    if seen_no_ts:
        emit(f"seen but no tlast timestamp           {len(seen_no_ts):5d}"
             "   (series/query disagreement)")
        for entity in seen_no_ts[:10]:
            emit(f"      {entity}")
    emit()

    # -- guards --------------------------------------------------------------
    emit("-- GUARDS (pre-registered before the run; a failure means the instrument "
         "is broken) --")
    failures: list[str] = []

    g1_ok = G1_MIN <= len(ha) <= G1_MAX
    emit(f"G1 HA side plausible          {'PASS' if g1_ok else 'FAIL'}"
         f"   {len(ha)} entities in [{G1_MIN}, {G1_MAX}]")
    if not g1_ok:
        failures.append(f"G1: HA returned {len(ha)} entities, expected {G1_MIN}-{G1_MAX}")

    g2_ok = len(vm_seen) >= G2_MIN
    emit(f"G2 VM side non-empty          {'PASS' if g2_ok else 'FAIL'}"
         f"   {len(vm_seen)} distinct (domain, entity_id) >= {G2_MIN}")
    if not g2_ok:
        failures.append(f"G2: VM holds {len(vm_seen)} pairs, expected >= {G2_MIN}")

    # G3 -- positive control. Entities that genuinely changed state just before
    # `end` MUST be in the VM-seen set; if they are not, the join key is wrong
    # (the exact failure #171 comment 3 warns about: entity_id carries the object
    # id only, so joining on it alone reports every entity as missing).
    g3_window = G3_WINDOW_S
    def control_set(width: int) -> list[str]:
        return sorted(
            e
            for e in writable
            if ha[e]["last_changed"] is not None
            and end - width <= ha[e]["last_changed"] <= end
            and numeric_capable(ha[e]["state"])
        )

    control = control_set(g3_window)
    widened = False
    if len(control) < G3_MIN_CONTROL:
        g3_window = G3_WIDENED_WINDOW_S
        control = control_set(g3_window)
        widened = True
    hits = [e for e in control if e in vm_seen]
    hit_rate = (len(hits) / len(control)) if control else 0.0
    g3_ok = len(control) >= G3_MIN_CONTROL and hit_rate >= G3_MIN_HIT_RATE
    emit(f"G3 join proven (pos. control) {'PASS' if g3_ok else 'FAIL'}"
         f"   {len(hits)}/{len(control)} = {hit_rate * 100:.1f}% of numeric-capable "
         f"entities changed in the last {g3_window // 60}min are VM-seen "
         f"(bar {G3_MIN_HIT_RATE * 100:.0f}%, min set {G3_MIN_CONTROL})")
    if widened:
        emit("   NOTE: control window widened from 30min to 2h -- the 30min set was "
             f"smaller than {G3_MIN_CONTROL}.")
    if not g3_ok:
        failures.append(
            f"G3: control set {len(control)} (min {G3_MIN_CONTROL}), hit rate "
            f"{hit_rate * 100:.1f}% (bar {G3_MIN_HIT_RATE * 100:.0f}%)"
        )
    misses = sorted(set(control) - set(hits))
    for entity in misses[:10]:
        emit(f"   control MISS: {entity} (state={ha[entity]['state']!r})")
    # Restore-burst indicator: HA rewrites last_changed at restore, which would
    # inflate the control set with entities that did not really change. A sane
    # instance has a small fraction recently-changed.
    recent_all = sum(
        1
        for rec in ha.values()
        if rec["last_changed"] is not None and end - g3_window <= rec["last_changed"] <= end
    )
    emit(f"   restore-burst indicator: {recent_all}/{len(ha)} "
         f"({recent_all / len(ha) * 100:.1f}%) of ALL entities report last_changed "
         f"inside the same {g3_window // 60}min -- a restore rewrites this to ~100%.")

    # P1 -- structural invariant, added after the stop-point-1 review rather than
    # pre-registered with G1-G5. Every VM-seen pair must land in exactly one
    # bucket; an unexplained remainder means entities are being silently absorbed
    # by the report instead of reported.
    partition = (
        len(seen_writable) + len(seen_now_absent) + len(seen_now_update) + len(vm_only)
    )
    p1_ok = partition == len(vm_seen)
    emit(f"P1 VM-seen partition sums     {'PASS' if p1_ok else 'FAIL'}"
         f"   {len(seen_writable)} writable + {len(seen_now_absent)} now-absent + "
         f"{len(seen_now_update)} update + {len(vm_only)} vm_only = {partition} "
         f"vs {len(vm_seen)} distinct pairs")
    if not p1_ok:
        failures.append(
            f"P1: buckets sum to {partition} but VM holds {len(vm_seen)} distinct "
            "pairs -- some VM-seen entities are unaccounted for"
        )

    g4_domain_hits = len(update_domain_series)
    g4_name_hits = len(update_prefixed_names)
    g4_ok = g4_domain_hits == 0 and g4_name_hits == 0
    emit(f"G4 update-domain absent       {'PASS' if g4_ok else 'FAIL'}"
         f"   {g4_domain_hits} series with domain=\"update\", {g4_name_hits} of "
         f"{len(metric_names)} metric names start 'update.'")
    if not g4_ok:
        failures.append(
            f"G4: domain=update series {g4_domain_hits}, update.* metric names "
            f"{g4_name_hits} -- the by-design subtraction is INVALID and is itself "
            "a finding"
        )
        for name in update_prefixed_names[:10]:
            emit(f"   update-prefixed name: {name}")
    emit()

    if failures:
        emit("=" * 78)
        for line in failures:
            emit(f"GUARD FAILED {line}")
        emit("A failed guard is an instrument failure. Fix the instrument; do NOT "
             "publish a dead list from this run.")
        emit("=" * 78)
        print("\n".join(out))
        return 2

    # -- never-seen numeric candidates ---------------------------------------
    emit("-- DEAD-OR-DORMANT CANDIDATES: writable, numeric-capable, NEVER seen in "
         "the window --")
    emit(f"   Silence is bounded BELOW by the window ({window / 3600:.1f}h): each of "
         "these is a candidate, not a conviction.")
    by_domain: dict[str, list[str]] = {}
    for entity in never_seen_numeric:
        by_domain.setdefault(ha[entity]["domain"], []).append(entity)
    for domain in sorted(by_domain):
        entities = sorted(by_domain[domain])
        emit(f"  {domain} ({len(entities)}):")
        for entity in entities:
            emit(f"      {entity}  state={ha[entity]['state']!r}")
    if not never_seen_numeric:
        emit("  (none)")
    emit()

    # -- ranked seen entities -------------------------------------------------
    emit("-- TOP 30 VM-SEEN WRITABLE ENTITIES BY SILENCE (end - last sample) --")
    ranked = sorted(silence.items(), key=lambda kv: (-kv[1], kv[0]))[:30]
    for entity, quiet in ranked:
        emit(f"  {fmt_silence(quiet):>8}  {entity}  state={ha[entity]['state']!r}")
    if not ranked:
        emit("  (none)")
    emit()

    # -- G6 structural invisibility -------------------------------------------
    emit("-- STRUCTURAL INVISIBILITY (G6 measurement, not a guard) --")
    emit("   A state that is neither float()-able nor in HA's binary vocabulary can "
         "only be stored")
    emit("   as a string field, so such an entity can be alive and still invisible "
         "to this diff.")
    string_by_domain: dict[str, int] = {}
    for entity in never_seen_string:
        string_by_domain[ha[entity]["domain"]] = (
            string_by_domain.get(ha[entity]["domain"], 0) + 1
        )
    for domain in sorted(string_by_domain):
        emit(f"   never-seen, string-only  {domain:<16} {string_by_domain[domain]:4d}"
             "   INCONCLUSIVE")
    if not string_by_domain:
        emit("   (no never-seen entity carries a string-only state)")
    # Empirical check on `select`: a domain whose states are always strings.
    select_changed_in_window = sorted(
        e
        for e in ha
        if ha[e]["domain"] == "select"
        and ha[e]["last_changed"] is not None
        and start <= ha[e]["last_changed"] <= end
    )
    select_vm_series = sum(1 for m in series if m.get("domain") == "select")
    emit(f"   empirical: {len(select_changed_in_window)} `select` entities changed "
         f"state inside the window; VM holds {select_vm_series} `select` series.")
    if select_changed_in_window and select_vm_series == 0:
        emit("   -> STRUCTURAL INVISIBILITY CONFIRMED for string states: real state "
             "changes, zero series.")
    elif not select_changed_in_window:
        emit("   -> not demonstrated: no `select` changed in-window, so its absence "
             "explains itself (proves nothing).")
    else:
        emit("   -> NOT confirmed: `select` series exist, so string states are not "
             "uniformly invisible.")
    emit()

    # -- vm_only ---------------------------------------------------------------
    emit(f"-- VM-SEEN WITH NO HA ENTITY ({len(vm_only)}) -- renamed or deleted in HA "
         "since the sample --")
    for entity in vm_only:
        if entity in last_sample:
            emit(f"   {entity}  silence={fmt_silence(end - last_sample[entity])}")
        else:
            emit(f"   {entity}")
    if not vm_only:
        emit("   (none)")
    emit()

    emit(f"-- VM-SEEN BUT NOW UNAVAILABLE/UNKNOWN ({len(seen_now_absent)}) -- reported "
         "in-window, gone now --")
    emit("   The freshest-dead / flapping shape: VictoriaMetrics holds a sample inside "
         "the window,")
    emit("   yet HA currently reports a state it never writes. Not in the never-seen "
         "list by construction.")
    for entity in seen_now_absent:
        quiet = (
            fmt_silence(end - last_sample[entity]) if entity in last_sample else "?"
        )
        emit(f"   {quiet:>8}  {entity}  state={ha[entity]['state']!r}")
    if not seen_now_absent:
        emit("   (none)")
    emit()

    # -- nine-device cross-check ----------------------------------------------
    emit("-- CROSS-CHECK vs the nine `last_seen`-derived dead devices (#133) --")
    emit("   Two independent mechanisms. If they disagree, the disagreement is the "
         "finding.")
    for device, days, since in NINE_DEVICES:
        matches = sorted(e for e in ha if device in e.split(".", 1)[1])
        emit(f"   {days:>5}d  {device}  (last_seen: {since})")
        if not matches:
            emit("            VERDICT: ABSENT-FROM-HA -- no entity's object_id "
                 "contains this name (deleted from HA)")
            continue
        any_seen = False
        for entity in matches:
            if entity in excluded_update:
                category = "excluded(update)"
            elif entity in absent_by_design:
                category = "absent-by-design"
            else:
                category = "writable"
            if entity in vm_seen:
                any_seen = True
                quiet = (
                    fmt_silence(end - last_sample[entity])
                    if entity in last_sample
                    else "?"
                )
                vm_status = f"VM-SEEN silence={quiet}"
            else:
                vm_status = "never-seen"
            emit(f"            {entity}  [{category}]  {vm_status}  "
                 f"state={ha[entity]['state']!r}")
        if any_seen:
            emit("            VERDICT: DISAGREE -- VictoriaMetrics holds a sample "
                 f"inside the {window / 3600:.1f}h window for a device `last_seen` "
                 "calls dead")
        else:
            all_absent = all(e in absent_by_design for e in matches)
            writable_never = [
                e for e in matches if e in writable_set and e not in vm_seen
            ]
            if all_absent:
                emit("            VERDICT: AGREE -- every matching entity is "
                     "unavailable/unknown; HA knows it is gone")
            elif writable_never:
                emit("            VERDICT: AGREE (stronger) -- HA still reports a "
                     "live state, yet VM has never seen it in-window")
            else:
                emit("            VERDICT: AGREE -- no sample in the window")
    emit()

    emit("=" * 78)
    emit(f"WINDOW: {iso(start)} .. {iso(end)} ({window / 3600:.1f}h) -- findings are "
         "bounded by this window")
    emit("=" * 78)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

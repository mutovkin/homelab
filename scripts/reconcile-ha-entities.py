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

Exit codes: 0 = all guards passed; 1 = a fatal error before the verdict (bad
credentials, unreachable endpoint, malformed payload, unusable snapshot);
2 = a guard failed, and no dead list was printed.
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
# How far back the start auto-detection probes. Data older than this is invisible
# to it, so `saturated` reports the cap rather than presenting it as the answer.
DETECT_LOOKBACK_S = 8 * 86400
# How far an HA snapshot may sit from --end before it describes a different
# instance than the VictoriaMetrics window does.
SNAPSHOT_SKEW_TOLERANCE_S = 900

HTTP_TIMEOUT = 120


class Failure(RuntimeError):
    """Fatal, operator-readable error. Never carries a credential."""


class Secret:
    """A credential that cannot be printed by accident.

    CPython's default excepthook does not dump locals, so a bare string was
    already safe on every path here -- but a credential travelling as a plain
    positional argument through four stack frames is one `capture_locals=True`
    (pytest, rich, cgitb) away from being echoed verbatim. The redacting repr
    closes that off permanently.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:  # noqa: D105 - the whole point
        return "<Secret redacted>"

    __str__ = __repr__


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
            # `key: >` / `key: |` is a BLOCK SCALAR whose value lives on the
            # following indented lines. A line-based parser would hand back the
            # indicator itself, which is non-empty and would sail through to an
            # Authorization header -- surfacing as a 401 that names the wrong
            # cause. Reject the shape instead.
            if value in {">", "|", ">-", "|-", ">+", "|+", "{", "["}:
                raise Failure(
                    f"{key} in {where} looks like a YAML block/flow indicator "
                    f"({value!r}), not a value -- this parser reads scalars on one "
                    "line only. Rewrite it as a single-line quoted scalar."
                )
            return value
    raise Failure(f"{key} not found in {where}")


# ---------------------------------------------------------------------------
# HTTP helpers. Exceptions carry the URL PATH and the status only -- never the
# query string, never a header, so no Authorization value can leak into a
# traceback or a CI log.
# ---------------------------------------------------------------------------


def _request(req: urllib.request.Request, what: str) -> dict | list:
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raise Failure(f"{what}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise Failure(f"{what}: unreachable ({exc.reason})") from None
    except TimeoutError:
        raise Failure(f"{what}: timed out after {HTTP_TIMEOUT}s") from None
    except OSError as exc:
        # A reset or short read mid-body lands here rather than as a traceback.
        raise Failure(f"{what}: connection failed ({type(exc).__name__})") from None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise Failure(f"{what}: response was not JSON ({len(payload)} bytes)") from None


def ha_get(ha_url: str, path: str, token: Secret) -> list | dict:
    try:
        req = urllib.request.Request(
            ha_url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {token.reveal()}",
                     "Accept": "application/json"},
            method="GET",
        )
    except ValueError as exc:
        raise Failure(f"HA GET {path}: bad --ha-url ({exc})") from None
    return _request(req, f"HA GET {path}")


def vm_post(
    vm_url: str, path: str, params: list[tuple[str, str]], auth: Secret
) -> dict:
    """POST to a VictoriaMetrics READ endpoint (query / query_range / series)."""
    body = urllib.parse.urlencode(params).encode()
    try:
        req = urllib.request.Request(
            vm_url.rstrip("/") + path,
            data=body,
            headers={
                "Authorization": f"Basic {auth.reveal()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
    except ValueError as exc:
        raise Failure(f"VM POST {path}: bad --vm-url ({exc})") from None
    doc = _request(req, f"VM POST {path}")
    if not isinstance(doc, dict):
        raise Failure(f"VM POST {path}: response was not a JSON object")
    if doc.get("status") != "success":
        raise Failure(f"VM POST {path}: status={doc.get('status')!r}")
    # A single-node VictoriaMetrics never sets isPartial, but a 200 carrying a
    # partial body is exactly the "looks fine, measured less than it should"
    # shape this whole script exists to refuse. One line, checked anyway.
    if doc.get("isPartial"):
        raise Failure(
            f"VM POST {path}: response is marked isPartial -- a partial answer "
            "cannot support an absence finding"
        )
    return doc


def vm_result(doc: dict, path: str) -> list:
    """`data.result` from a VictoriaMetrics response, or a named Failure.

    Without this, a malformed-but-"success" payload was a silent empty list in
    one helper and a raw KeyError (exit 1, traceback) in two others -- the same
    server condition reported three different ways, one of them invisibly.
    """
    data = doc.get("data")
    if not isinstance(data, dict) or "result" not in data:
        raise Failure(
            f"VM POST {path}: response has status=success but no data.result -- "
            "refusing to read a malformed payload as an empty measurement"
        )
    result = data["result"]
    if not isinstance(result, list):
        raise Failure(f"VM POST {path}: data.result is not a list")
    return result


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
    if not isinstance(state, str):
        # HA can serve "state": null; .lower() would raise on it.
        return False
    try:
        value = float(state)
    except (TypeError, ValueError):
        return state.lower() in BINARY_STATE_VOCABULARY
    # NaN/inf are float()-able and would poison max() and the silence formatter.
    return value == value and value not in (float("inf"), float("-inf"))


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


def detect_start(vm_url: str, auth: Secret, end: int) -> tuple[int, bool]:
    """First 30-min bucket of {db="ha"} that has a value, minus one bucket of
    alignment slop. Returns (start, saturated).

    The probe only looks back DETECT_LOOKBACK_S. Once the data outlives that, the
    first populated bucket IS the probe's own left edge, and "widest available
    window" silently becomes "the probe's lookback" -- a cap presented as a
    measurement. `saturated` says so, and the caller prints it.
    """
    probe_start = end - DETECT_LOOKBACK_S
    doc = vm_post(
        vm_url,
        "/api/v1/query_range",
        [
            ("query", 'count({db="ha"})'),
            ("start", str(probe_start)),
            ("end", str(end)),
            ("step", "1800"),
        ],
        auth,
    )
    result = vm_result(doc, "/api/v1/query_range")
    if not result:
        raise Failure(
            'auto-detect start: count({db="ha"}) returned no series over the last '
            f"{DETECT_LOOKBACK_S // 86400} days -- is the HA push alive?"
        )
    stamps = sorted(int(point[0]) for series in result for point in series["values"])
    if not stamps:
        raise Failure('auto-detect start: count({db="ha"}) has no populated bucket')
    # Within two buckets of the probe edge means the data very likely predates it.
    saturated = stamps[0] <= probe_start + 3600
    return stamps[0] - 1800, saturated


def fetch_ha_states(ha_url: str, token: Secret) -> list[dict]:
    states = ha_get(ha_url, "/api/states", token)
    if not isinstance(states, list):
        raise Failure("HA /api/states did not return a list")
    if not states:
        # Without this, len(ha)==0 divides by zero in the restore-burst indicator
        # BEFORE the guard block can print "G1 FAIL 0 entities" and exit 2 -- the
        # guard's own broken case would crash instead of reporting.
        raise Failure("HA /api/states returned an empty list")
    return states


def git_worktrees_containing(path: Path) -> list[Path]:
    """Every git work tree that would track `path`, resolved from `path` itself.

    Testing containment against the SCRIPT's repo root is not enough here: this
    repo drives its agents from worktrees under `.claude/worktrees/<id>/`, which
    are subdirectories of the main checkout. A snapshot written to the main
    checkout's root is outside the worktree, passes a root-relative test, and
    lands in a tracked tree anyway. Ask git about the destination instead.
    """
    probe = path if path.is_dir() else path.parent
    roots: list[Path] = []
    for flag in ("--show-toplevel", "--git-common-dir"):
        try:
            proc = subprocess.run(
                ["git", "-C", str(probe), "rev-parse", flag],
                capture_output=True, text=True, check=False,
            )
        except (FileNotFoundError, OSError):
            return roots
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        found = Path(proc.stdout.strip()).resolve()
        # --git-common-dir points at `<main checkout>/.git`; its parent is the
        # tree that would track the file.
        roots.append(found.parent if found.name == ".git" else found)
    return roots


def load_or_capture_ha_states(
    ha_url: str, token: Secret, snapshot: Path | None, repo_root: Path, end: int
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
    # It must never land in a tracked tree, where a stray `git add -A` publishes it.
    forbidden = [repo_root, *git_worktrees_containing(snapshot)]
    for root in forbidden:
        if snapshot == root or root in snapshot.parents:
            raise Failure(
                f"refusing to write an HA snapshot inside a git work tree "
                f"({root}): /api/states carries personal home-state data. Put it "
                "in a scratch directory outside every checkout."
            )

    if snapshot.exists() and not snapshot.is_file():
        raise Failure(
            f"HA snapshot path {snapshot} exists and is not a regular file"
        )

    if snapshot.is_file():
        try:
            payload = json.loads(snapshot.read_text())
        except json.JSONDecodeError as exc:
            raise Failure(
                f"HA snapshot {snapshot} is not valid JSON ({exc.msg}) -- an "
                "interrupted capture leaves a truncated file; delete it and re-run"
            ) from None
        if not isinstance(payload, dict) or "states" not in payload:
            raise Failure(
                f"HA snapshot {snapshot} is not in the expected "
                '{"captured_at": <epoch>, "states": [...]} form'
            )
        states = payload["states"]
        captured_at = payload.get("captured_at")
        if not isinstance(states, list) or not states:
            raise Failure(f"HA snapshot {snapshot} does not hold a non-empty list")
        if not isinstance(captured_at, (int, float)):
            raise Failure(f"HA snapshot {snapshot} has no usable captured_at stamp")
        # A snapshot taken far from `end` describes a different instance than the
        # VictoriaMetrics window does: entities created, renamed or deleted in
        # between land in never-seen and vm_only as pure artefacts. A grossly
        # stale one happens to empty G3's control set, but that backstop is
        # accidental and does not cover the near-stale regime.
        skew = abs(captured_at - end)
        if skew > SNAPSHOT_SKEW_TOLERANCE_S:
            raise Failure(
                f"HA snapshot {snapshot} was captured {skew:.0f}s from --end "
                f"(tolerance {SNAPSHOT_SKEW_TOLERANCE_S}s). Re-capture it, or pin "
                "--end to the capture time; a skewed snapshot invents findings."
            )
        print(f"HA states REPLAYED from snapshot {snapshot} ({len(states)} entities, "
              f"captured {skew:.0f}s from --end)", file=sys.stderr)
        return states

    states = fetch_ha_states(ha_url, token)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600: see the personal-data note above.
    fd = os.open(str(snapshot), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"captured_at": int(time.time()), "states": states}, handle)
    print(f"HA states CAPTURED to snapshot {snapshot} ({len(states)} entities)",
          file=sys.stderr)
    return states


def fetch_vm_series(
    vm_url: str, auth: Secret, start: int, end: int, match: str
) -> list[dict]:
    doc = vm_post(
        vm_url,
        "/api/v1/series",
        [("match[]", match), ("start", str(start)), ("end", str(end))],
        auth,
    )
    data = doc.get("data")
    if data is None or not isinstance(data, list):
        raise Failure(
            f"VM POST /api/v1/series ({match}): status=success but data is "
            f"{type(data).__name__} -- refusing to read that as zero series"
        )
    return data


def fetch_vm_last_samples(
    vm_url: str, auth: Secret, start: int, end: int
) -> tuple[dict[str, float], int]:
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
    dropped = 0
    for item in vm_result(doc, "/api/v1/query"):
        metric = item.get("metric", {})
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if not domain or not entity:
            dropped += 1
            continue
        ts = float(item["value"][1])
        if ts != ts:  # NaN would poison max() and crash fmt_silence later
            dropped += 1
            continue
        key = f"{domain}.{entity}"
        out[key] = max(out.get(key, 0.0), ts)
    return out, dropped


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
    vm_auth = Secret(base64.b64encode(f"{vm_user}:{vm_pass}".encode()).decode())
    ha_token = Secret(ha_token)
    del vm_pass

    # -- window --------------------------------------------------------------
    # The ONLY wallclock read in the script; everything downstream is a function
    # of (start, end), which is what makes a pinned re-run reproducible.
    now = int(time.time())
    end = args.end if args.end is not None else now
    if end > now + 300:
        # A future --end inflates every silence figure uniformly and still clears
        # G3, so it fails no guard while making the whole report wrong.
        raise Failure(
            f"--end {end} is {end - now}s in the future; silence figures would be "
            "inflated by that much and no guard would catch it"
        )
    saturated = False
    if args.start is not None:
        start = args.start
    else:
        start, saturated = detect_start(args.vm_url, vm_auth, end)
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
    emit(f"           --start {start} --end {end}")
    if args.ha_states_json:
        emit("           HA input PINNED to a snapshot -- with the same snapshot, "
             "this output is byte-identical.")
    else:
        emit("           NOT byte-reproducible: HA /api/states is live and its "
             "last_changed drifts (~11 entities/min),")
        emit("           which moves the G3 control size and the restore-burst "
             "line. Add --ha-states-json to pin it.")
    if saturated:
        emit(f"           WARNING: start is at the auto-detection probe's own edge "
             f"({DETECT_LOOKBACK_S // 86400}d).")
        emit("           This window is capped by the PROBE, not by the data -- "
             "pass --start explicitly to go wider.")
    emit(f"HA       : {args.ha_url}/api/states")
    emit(f"VM       : {args.vm_url}  (series + tlast_over_time, read-only)")
    emit()

    # -- HA side -------------------------------------------------------------
    states = load_or_capture_ha_states(
        args.ha_url,
        ha_token,
        Path(args.ha_states_json) if args.ha_states_json else None,
        repo_root,
        end,
    )
    del ha_token

    ha: dict[str, dict] = {}
    dropped_no_dot = 0
    unparseable_last_changed = 0
    for item in states:
        full_id = item.get("entity_id", "")
        if not isinstance(full_id, str) or "." not in full_id:
            dropped_no_dot += 1
            continue
        changed = parse_ha_ts(item.get("last_changed", ""))
        if changed is None:
            # Counted, not swallowed: a PARTIAL last_changed format change would
            # otherwise shrink G3's control set and DEFLATE the restore-burst
            # indicator -- making a restore look less like a restore, on the one
            # number whose job is to detect exactly that.
            unparseable_last_changed += 1
        ha[full_id] = {
            "domain": full_id.split(".", 1)[0],
            "state": item.get("state", ""),
            "last_changed": changed,
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
    index_seen: set[str] = set()
    unjoinable = []
    nameless_series = 0
    metric_names: set[str] = set()
    for metric in series:
        name = metric.get("__name__", "")
        if name:
            metric_names.add(name)
        else:
            nameless_series += 1
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if domain and entity:
            index_seen.add(f"{domain}.{entity}")
        else:
            unjoinable.append(name or "<unnamed>")

    last_sample, tlast_dropped = fetch_vm_last_samples(args.vm_url, vm_auth, start, end)

    # The two VM endpoints disagree in BOTH directions, for different reasons:
    #   index_only -- /api/v1/series resolves against a per-DAY inverted index, so
    #                 it can list a series whose samples fall outside [start,end];
    #                 or a sample landed so recently it is not queryable yet
    #                 (measured: present in /api/v1/export at T, invisible to a
    #                 query at T+5s).
    #   query_only -- tlast_over_time returned a real in-window sample timestamp
    #                 for a pair the index did not list.
    # Taking the UNION is the conservative direction: an entity either endpoint
    # saw is never printed as dead. Reading only the index would have let a
    # query_only entity into the dead list with its own proof of life sitting
    # unread in memory.
    vm_seen = index_seen | set(last_sample)
    index_only = sorted(index_seen - set(last_sample))
    query_only = sorted(set(last_sample) - index_seen)

    update_domain_series = fetch_vm_series(
        args.vm_url, vm_auth, start, end, '{db="ha",domain="update"}'
    )
    # G4 positive control: the SAME code path, same argument shape, against a
    # domain known to be present. Without it, G4's healthy answer (0 series) and
    # a silently broken query (0 series) are the same number.
    g4_control_series = fetch_vm_series(
        args.vm_url, vm_auth, start, end, '{db="ha",domain="sensor"}'
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
    # Writable entities the index listed but tlast could not date. NOT a weaker
    # signal than never-seen and not a stronger one -- it is UNRESOLVED, and the
    # two causes point opposite ways (see the union comment above). Listed in
    # full, never truncated: truncating this list is how the earlier version hid
    # them.
    seen_writable_undated = sorted(e for e in seen_writable if e not in last_sample)

    # -- counts --------------------------------------------------------------
    emit("-- COUNTS " + "-" * 67)
    emit(f"HA entities total                     {len(ha):5d}")
    emit(f"  excluded by design (domain=update)  {len(excluded_update):5d}"
         f"   (of which unavailable/unknown: {overlap_update_absent})")
    emit(f"  absent by design (unavail/unknown)  {len(absent_by_design):5d}"
         "   HA never writes these: event_to_json -> None")
    emit(f"  writable                            {len(writable):5d}")
    emit(f"  entity_id unusable (no domain dot)  {dropped_no_dot:5d}   dropped")
    emit(f"  last_changed unparseable            {unparseable_last_changed:5d}"
         "   G3/restore-burst read past these")
    emit(f"VM series over window                 {len(series):5d}"
         f"   ({len(metric_names)} distinct metric names)")
    emit(f"  series with no __name__             {nameless_series:5d}")
    emit(f"  series lacking domain or entity_id  {len(unjoinable):5d}   (cannot join)")
    for name in sorted(set(unjoinable))[:10]:
        emit(f"      {name}")
    emit(f"  tlast results dropped (labels/NaN)  {tlast_dropped:5d}")
    emit(f"VM distinct (domain, entity_id)       {len(vm_seen):5d}"
         "   union of both endpoints")
    emit(f"  listed by index, undated by query   {len(index_only):5d}")
    emit(f"  dated by query, not listed by index {len(query_only):5d}")
    emit(f"writable AND VM-seen                  {len(seen_writable):5d}")
    emit(f"  of which undated (no tlast)         {len(seen_writable_undated):5d}"
         "   UNRESOLVED, not ranked")
    emit(f"VM-seen, now unavailable/unknown      {len(seen_now_absent):5d}"
         "   seen in-window, HA reports it gone now")
    emit(f"VM-seen, domain=update                {len(seen_now_update):5d}"
         "   expected 0 (G4 subtraction)")
    emit(f"writable NEVER seen                   {len(never_seen):5d}")
    emit(f"  numeric-capable state (candidates)  {len(never_seen_numeric):5d}")
    emit(f"  string-only state (INCONCLUSIVE)    {len(never_seen_string):5d}")
    emit(f"VM-seen with no HA entity (vm_only)   {len(vm_only):5d}")
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
    emit(f"   NOTE: {G2_MIN} was pre-registered against a 6.8h window and this one "
         f"is {window / 3600:.1f}h, so the floor is slack here --")
    emit("   a large but non-total shortfall would still pass. G3 cannot cover "
         "that (its control set is by")
    emit("   construction the FRESHEST entities); the cross-endpoint check below "
         "is what covers it.")
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
    burst_pct = (recent_all / len(ha) * 100) if ha else 0.0
    emit(f"   restore-burst indicator: {recent_all}/{len(ha)} "
         f"({burst_pct:.1f}%) of ALL entities report last_changed "
         f"inside the same {g3_window // 60}min -- a restore rewrites this to ~100%.")
    if unparseable_last_changed:
        emit(f"   CAVEAT: {unparseable_last_changed} entities have an unparseable "
             "last_changed and are excluded from the numerator,")
        emit("   which DEFLATES this indicator -- the one number whose job is to "
             "detect that HA rewrote the field.")

    # P1 -- a SOURCE-CODE regression canary, not a data guard, and labelled as one.
    # The three HA buckets come from a single if/elif/else over unique dict keys,
    # so this identity holds for every possible input; it can only fail if someone
    # edits the bucketing. That is worth keeping (it is how the dropped bucket was
    # demonstrated) but it is not evidence about THIS run's data, and it does not
    # catch an entity being absorbed WITHIN a bucket.
    partition = (
        len(seen_writable) + len(seen_now_absent) + len(seen_now_update) + len(vm_only)
    )
    p1_ok = partition == len(vm_seen)
    emit(f"P1 bucket-code canary         {'PASS' if p1_ok else 'FAIL'}"
         f"   {len(seen_writable)} writable + {len(seen_now_absent)} now-absent + "
         f"{len(seen_now_update)} update + {len(vm_only)} vm_only = {partition} "
         f"vs {len(vm_seen)} distinct pairs")
    emit("   (structural: true for all inputs by construction -- fails only on a "
         "bucketing-code edit, not on data)")
    if not p1_ok:
        failures.append(
            f"P1: buckets sum to {partition} but VM holds {len(vm_seen)} distinct "
            "pairs -- the bucketing code has been broken"
        )

    # P2 -- the cross-endpoint check, and the one that CAN fail on data. Every
    # entity tlast dated must be in the seen set, so no entity can reach the dead
    # list while its own sample timestamp sits unread in memory.
    leaked = sorted(set(last_sample) & set(never_seen))
    p2_ok = not leaked
    emit(f"P2 no dated entity called dead {'PASS' if p2_ok else 'FAIL'}"
         f"  {len(leaked)} of {len(never_seen)} never-seen entities have a "
         "tlast timestamp")
    for entity in leaked[:10]:
        emit(f"   LEAKED: {entity} last sample {iso(last_sample[entity])}")
    if not p2_ok:
        failures.append(
            f"P2: {len(leaked)} entities are in the dead list AND have an in-window "
            "sample timestamp -- the seen set is not reading both endpoints"
        )

    g4_domain_hits = len(update_domain_series)
    g4_name_hits = len(update_prefixed_names)
    g4_control_hits = len(g4_control_series)
    # An expect-zero guard whose query is broken also returns zero. The control
    # runs the SAME helper with the same argument shape against a domain known to
    # be present, so "0 because absent" and "0 because broken" stop looking alike.
    g4_ok = g4_domain_hits == 0 and g4_name_hits == 0 and g4_control_hits > 0
    emit(f"G4 update-domain absent       {'PASS' if g4_ok else 'FAIL'}"
         f"   {g4_domain_hits} series with domain=\"update\", {g4_name_hits} of "
         f"{len(metric_names)} metric names start 'update.'")
    emit(f"   positive control (same code path, domain=\"sensor\"): "
         f"{g4_control_hits} series -- must be > 0, else the zero above is "
         "meaningless")
    if g4_control_hits == 0:
        failures.append(
            "G4: the positive control returned 0 series for domain=\"sensor\", so "
            "the zero for domain=\"update\" proves nothing about the subtraction"
        )
    if not g4_ok:
        failures.append(
            f"G4: domain=update series {g4_domain_hits}, update.* metric names "
            f"{g4_name_hits} -- the by-design subtraction is INVALID and is itself "
            "a finding"
        )
        for name in update_prefixed_names[:10]:
            emit(f"   update-prefixed name: {name}")
    emit()

    g5_pinned = bool(args.ha_states_json)
    emit(f"G5 determinism precondition   {'PASS' if g5_pinned else 'N/A '}"
         f"   HA input {'PINNED to a snapshot' if g5_pinned else 'is LIVE'} -- "
         "byte-identity requires --ha-states-json")
    emit("   (not a failure: an unpinned run is still valid, its G3 control line "
         "just will not repeat)")
    emit()

    if failures:
        emit("=" * 78)
        for line in failures:
            emit(f"GUARD FAILED {line}")
        emit("A failed guard is an instrument failure. Fix the instrument; do NOT "
             "publish a dead list from this run.")
        emit("=" * 78)
        # Banner at the TOP too: the counts print before the guards, so the
        # numbers a reader would paste sit above the verdict without it.
        banner = [
            "!" * 78,
            "!! GUARDS FAILED -- EVERY NUMBER BELOW IS UNTRUSTWORTHY. DO NOT PUBLISH.",
            "!" * 78,
            "",
        ]
        print("\n".join(banner + out))
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

    emit(f"-- VM-SEEN BUT UNDATED ({len(seen_writable_undated)}) -- listed by the "
         "series index, no tlast timestamp --")
    emit("   UNRESOLVED, deliberately not ranked. Two causes point OPPOSITE ways and "
         "this run cannot")
    emit("   separate them: /api/v1/series resolves against a per-DAY index and may "
         "list a series whose")
    emit("   samples fall outside the window (=> staler than it looks), or a sample "
         "landed too recently")
    emit("   to be queryable (=> fresher). Measured: a sample in /api/v1/export at "
         "T was invisible to a")
    emit("   query at T+5s. Full list, never truncated:")
    for entity in seen_writable_undated:
        emit(f"   {entity}  state={ha[entity]['state']!r}")
    if not seen_writable_undated:
        emit("   (none)")
    emit()

    # -- G6 structural invisibility -------------------------------------------
    emit("-- STRUCTURAL INVISIBILITY (G6 measurement, not a guard) --")
    emit("   Pre-registered theory: a state that is neither float()-able nor in HA's "
         "binary vocabulary")
    emit("   can only be stored as a string field, so such an entity could be alive "
         "and still invisible.")
    seen_in_ha = [e for e in vm_seen if e in ha]
    seen_string = [e for e in seen_in_ha if not numeric_capable(ha[e]["state"])]
    emit(f"   MEASURED, and it does NOT hold: {len(seen_string)} of "
         f"{len(seen_in_ha)} VM-seen entities carry a non-numeric-capable state,")
    emit("   so string states ARE written. The rule below is applied as "
         "pre-registered anyway -- a rule fixed")
    emit("   before a run does not get rewritten after seeing the data -- but the "
         "refutation cuts toward MORE")
    emit("   suspicion of this group, not less: their absence is no longer excused "
         "by mechanism, just unranked.")
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
    def device_matches(pool) -> list[str]:
        # ANCHORED, not a bare substring: `stairs_sensor` is a substring of
        # `upstairs_sensor` and `downstairs_sensor`, and a live sibling matching
        # by accident would set any_seen and manufacture the DISAGREE verdict --
        # the headline this report treats as its most important possible result.
        # Verified against this instance: anchoring changes 98 matches to 98.
        out = []
        for full in pool:
            obj = full.split(".", 1)[1]
            if obj == device or obj.startswith(device + "_"):
                out.append(full)
        return sorted(out)

    for device, days, since in NINE_DEVICES:
        matches = device_matches(ha)
        emit(f"   {days:>5}d  {device}  (last_seen: {since})")
        if not matches:
            # Ask the side that can still answer: an entity deleted from HA may
            # well have samples in VictoriaMetrics, which is the one question
            # this script is uniquely able to settle.
            vm_matches = device_matches(vm_seen)
            if vm_matches:
                emit("            VERDICT: ABSENT-FROM-HA, PRESENT IN VM -- deleted "
                     "from HA, yet VictoriaMetrics holds in-window samples:")
                for entity in vm_matches:
                    quiet = (fmt_silence(end - last_sample[entity])
                             if entity in last_sample else "undated")
                    emit(f"                     {entity}  silence={quiet}")
            else:
                emit("            VERDICT: ABSENT-FROM-HA -- no entity's object_id "
                     "matches this name in HA or in VM (deleted)")
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

#!/usr/bin/env python3
"""Reconcile the Home Assistant entity set against what VictoriaMetrics stored.

Issue #171 item 2. Answers "which HA entities are dead?" WITHOUT trusting HA's own
metadata, because HA's `last_changed` is rewritten at restore: measured 2026-08-24,
0 of 574 not-yet-seen entities read older than 7 days, including one device dead
since 2023-08-20. VictoriaMetrics' sample timestamps are the only honest source.

THREAT MODEL, declared. This guard is ACCIDENT PREVENTION for a tool the
operator runs on their own machine. The failure it exists to stop is the one that
actually happened here: a snapshot of the house written into a tracked git tree,
where a stray `git add -A` would publish it. It is NOT a defence against an
adversary who controls the arguments, the environment or the filesystem -- anyone
who does has already won by simpler means than a crafted path.

Under that model these residuals are accepted and named rather than chased:
  * `core.worktree` set in a BARE repo's config, pointing into an approved root:
    not detected by either git probe.
  * PATH or GIT_CONFIG_* manipulation that suppresses the probes' widening.
  * TOCTOU between the checks and the write: the checks run on the canonical
    path, and O_EXCL protects only the final component, so a parent-directory
    symlink swapped in between is not covered.
  * Any path-resolution case that requires deliberately hostile input.

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

Reproducibility, stated precisely because the naive claim is false -- and the
first precise statement of it was still too strong. THREE sources move, not one.

1. HA's /api/states is a live source: an entity that changes state now moves its
   `last_changed` past a pinned `end`, which shrinks the G3 positive-control set
   (measured 2026-08-24: ~11 entities/min, the only two lines that moved across
   four pinned re-runs). --ha-states-json pins that side: the first run captures
   the payload, later runs replay it.
2. The two QUERY legs -- tlast_over_time and count_over_time -- ARE pure
   functions of (--start, --end) and repeat exactly.
3. /api/v1/series is NOT. It resolves against a per-UTC-DAY inverted index, so a
   window that ends mid-day picks up that day's whole bucket, which keeps growing
   until the day closes. Measured on one pinned 28.1h window: 245 union pairs at
   end+minutes, 300 at end+3.9h (index_only 0 -> 55, vm_only 0 -> 46), while both
   query legs held steady at 245. A 60-second window returns the same answer as
   the whole day -- that is the tell.

Which numbers that touches is wider than it looks, and an earlier version of this
note got it wrong by claiming the dead list was built from the query legs. It is
not: never_seen = writable - vm_seen, and vm_seen is the UNION, so the DEAD LIST
IS INDEX-DEPENDENT. Two magnitudes, and they are not the same number. With a STUB
(55 synthetic index pairs, all of them writable HA entities), the dead list moved
by the full 55 -- that is the mechanism, not a field measurement. In the FIELD,
the same 3.9h replay moved the dead list by 9: of the 55 real index_only pairs, 46
had no HA entity at all and landed in vm_only, leaving 9 writable ones to come out
of never-seen (479 -> 470). Read 9 as the observed magnitude and 55 as the upper
bound the mechanism allows. So are index_only, vm_only, the series and
metric-name counts, G4's series counts, G2's total, G3's hit rate, G6's
VM-seen/string split, and the nine-device cross-check -- every one reads vm_seen
or the raw series list.

What IS sound is the DIRECTION, and it is sound by condition, not by
construction:
  * While the data is RETAINED, vm_seen only grows as the trailing day fills, so
    a later re-run can only SHRINK the candidate list -- it never convicts anyone
    new. Retention is the qualifier that matters: if samples age out, or a series
    is deleted, vm_seen shrinks and the candidate list would GROW.
  * The published 479 is window-pure BECAUSE index_only was 0 in that run, which
    makes writable - vm_seen exactly equal to writable - TLAST-seen (index_only is
    defined against the tlast set); P3 passing is what extends that to the other
    query leg. That is a property of that run, verifiable from its printed counts,
    not a guarantee
    about every run.

The practical rule: RUN PROMPTLY after the window ends, and expect byte-identity
only across re-runs made against the same series-index state.

Snapshots carry personal home-state data, so where one may be written is decided
by an AFFIRMATIVE ALLOW-LIST, not by trying to recognise a checkout: the path is
canonicalized first, and must then lie under $TMPDIR, /tmp, /private/tmp, or a
directory named with --snapshot-allow-root. Everything else still runs behind
that gate (the ha-states*.json name pattern, a filesystem walk for a `.git`
ancestor, two git probes, O_EXCL + 0600), and any error, ambiguity, unlisted root
or failed probe is a refusal.

Usage:
    export ANSIBLE_VAULT_PASSWORD_FILE=/path/to/.vault_password
    scripts/reconcile-ha-entities.py                     # widest available window
    scripts/reconcile-ha-entities.py --ha-states-json /tmp/ha-states.json
    scripts/reconcile-ha-entities.py --start S --end E --ha-states-json "$TMPDIR/ha-states.json"
    # anywhere else needs the operator to approve the root explicitly:
    scripts/reconcile-ha-entities.py --snapshot-allow-root /data/scratch \
        --ha-states-json /data/scratch/ha-states.json

A PAST --end requires a snapshot captured at that time. The freshness check
compares the HA readout against `end` on EVERY path, so a live fetch (which can
only describe now) cannot serve a historical window -- and must not, because the
HA side would then describe a different instance than the VictoriaMetrics window
does. Capture first, replay against the pinned window afterwards.

Exit codes: 0 = all guards passed; 1 = a fatal error before the verdict (bad
credentials, unreachable endpoint, malformed payload, unusable snapshot);
2 = a guard failed, and no dead list was printed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.client
import json
import math
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
# How far the HA entity list may sit from --end before it describes a different
# instance than the VictoriaMetrics window does. Applies to live fetches, captures
# and replays alike.
HA_FRESHNESS_TOLERANCE_S = 900

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
            rest = line[len(prefix) :]
            stripped = rest.strip()
            quoted = stripped[:1] in ("'", '"')
            if quoted:
                # QUOTED: find the CLOSING quote and take what is inside. The
                # previous form required the LAST character of the line to be the
                # closing quote, so `vault_x: 'secret'  # rotated 2026-08-01` fell
                # into the unquoted branch and returned `'secret'` WITH the quotes
                # -- a 401 blaming the credential. All three live vault keys are
                # quoted, so that was one rotation comment away from happening.
                quote = stripped[0]
                close = stripped.find(quote, 1)
                if close == -1:
                    raise Failure(
                        f"{key} in {where} opens with {quote!r} and never closes "
                        "it on the same line -- this parser reads single-line "
                        "scalars only"
                    )
                if stripped[close + 1 : close + 2] == quote:
                    # `'it''s'` is a YAML-escaped quote; taking the first close
                    # would silently truncate. Refuse rather than guess.
                    raise Failure(
                        f"{key} in {where} contains an escaped quote "
                        f"({quote * 2!r}), which this single-line parser does not "
                        "decode. Rewrite it without one."
                    )
                value = stripped[1:close]
                trailer = stripped[close + 1 :].strip()
                if trailer and not trailer.startswith("#"):
                    raise Failure(
                        f"{key} in {where} has unparseable text after the closing "
                        f"quote ({trailer[:20]!r}...) -- refusing to guess where "
                        "the value ends"
                    )
            else:
                # UNQUOTED: a YAML scalar ends at a whitespace-`#`. Without this
                # cut, `vault_x: secret  # rotated 2026-08-01` hands the comment
                # to an Authorization header and the resulting 401 names the
                # wrong cause -- the same shape as the block-scalar trap below.
                cuts = [i for i in (rest.find(" #"), rest.find("\t#")) if i != -1]
                value = (rest[: min(cuts)] if cuts else rest).strip()
            if not value:
                raise Failure(f"{key} is empty in {where}")
            # `key: >` / `key: |` is a BLOCK SCALAR whose value lives on the
            # following indented lines. A line-based parser would hand back the
            # indicator itself, which is non-empty and would sail through to an
            # Authorization header -- surfacing as a 401 that names the wrong
            # cause. Reject the shape instead.
            if not quoted and value in {">", "|", ">-", "|-", ">+", "|+",
                                        "{", "["}:
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
    except http.client.HTTPException as exc:
        # IncompleteRead / BadStatusLine / LineTooLong are HTTPException, which is
        # NOT an OSError -- a truncated response body escaped the arm below as a
        # raw traceback until this was added.
        raise Failure(f"{what}: HTTP protocol error ({type(exc).__name__})") from None
    except OSError as exc:
        # A connection reset mid-body lands here rather than as a traceback. A
        # SHORT read does not: it is an http.client.HTTPException, caught above.
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
    # Clamped at zero. tlast values inside the (end, end+60] slack that
    # fetch_vm_last_samples deliberately tolerates produce a NEGATIVE silence,
    # and divmod walks backwards on a negative int ("-1d23h"). The slack exists
    # to absorb VictoriaMetrics' rounding, so the honest render of "sampled a few
    # seconds after `end`" is 0h00m, not a negative figure.
    seconds = max(0.0, seconds)
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
    try:
        stamps = sorted(
            int(point[0]) for series in result for point in series["values"]
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise Failure(
            "auto-detect start: query_range result is malformed "
            f"({type(exc).__name__}) -- refusing to guess a window from it"
        ) from None
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


# Directories a snapshot MAY be written under, before anything else is checked.
# See allow_roots() for why this list exists rather than a longer list of
# forbidden shapes.
DEFAULT_ALLOW_ROOTS = ("/tmp", "/private/tmp")

# Environment that can make `git rev-parse` deny standing in a repository it is
# in fact standing in. Stripped before the SANITIZED probe so the one remaining
# string match is stable by construction, together with LC_ALL=C. The second,
# INHERITED-env probe deliberately keeps them -- see git_worktrees_containing.
GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
)


def allow_roots(extra: list[str] | None) -> tuple[list[Path], list[str]]:
    """Resolved roots under which an HA snapshot may be written.

    AFFIRMATIVE ALLOW, and the inversion is the point. Four review rounds of this
    guard were an ENUMERATION of ways to be inside a checkout, and each round
    found one the last had missed. Round 4 found a shape with no `.git` anywhere
    in the destination's ancestry at all: a BARE repo plus an exported
    GIT_DIR/GIT_WORK_TREE (the dotfiles pattern -- and the exact environment git
    exports into every hook, `rebase --exec`, `bisect run` and `submodule
    foreach`), or `core.worktree` set in a bare repo's config. The filesystem walk
    passes, and a `git add -A` then stages the snapshot.

    An enumeration of forbidden shapes fails OPEN on the shape nobody enumerated.
    An allow-list fails CLOSED on it: an unanticipated layout lands on "not under
    an approved root", which is a refusal. Everything the enumeration checks is
    still checked -- this is a gate in front of them, not a replacement.

    Returns (roots, dropped): a candidate that cannot be used is REPORTED, not
    silently omitted, so a refusal can say which root was dropped and why.
    """
    candidates: list[tuple[str, str]] = []
    if os.environ.get("TMPDIR"):
        candidates.append((os.environ["TMPDIR"], "$TMPDIR"))
    candidates += [(item, "built-in") for item in DEFAULT_ALLOW_ROOTS]
    candidates += [(item, "--snapshot-allow-root") for item in (extra or [])]

    roots: list[Path] = []
    dropped: list[str] = []
    for item, source in candidates:
        if not item:
            continue
        # RELATIVE roots are refused, from every source. A relative $TMPDIR
        # resolves against the CWD, so running this from inside a checkout would
        # make the checkout an approved root -- the gate approving the one place
        # it exists to forbid.
        if not os.path.isabs(os.path.expanduser(item)):
            dropped.append(f"{item!r} ({source}): not an absolute path")
            continue
        # Resolved before comparison: on macOS $TMPDIR is /var/folders/... which
        # is really /private/var/folders/..., and an unresolved root would never
        # match a resolved destination.
        try:
            resolved = Path(os.path.realpath(os.path.expanduser(item)))
        except OSError as exc:
            dropped.append(f"{item!r} ({source}): cannot resolve ({exc.strerror})")
            continue
        # `/` as an allow-root makes the gate a TAUTOLOGY -- every path on the
        # machine is under it. Refused from every source, including an explicit
        # --snapshot-allow-root /.
        if resolved == Path("/"):
            dropped.append(
                f"{item!r} ({source}): resolves to '/', which would approve every "
                "path on the machine"
            )
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots, dropped


def canonical_path(path: Path) -> Path:
    """The canonical absolute path of a destination that may not exist yet.

    CANONICALIZE ONCE, FIRST, and let all three layers (the allow-root gate, the
    `.git` ancestor walk, the git-probe containment test) consume only this. A
    symlink sitting in an approved root and pointing into a checkout must be
    judged by where it LANDS, not by where it sits.

    BITTER LESSON, recorded so nobody re-derives it the hard way: do not
    hand-roll path canonicalization. `os.path.realpath` already resolves symlinks
    in the existing prefix AND collapses `..`, including after a component that
    does not exist. The previous revision replaced it with a bespoke
    deepest-existing-ancestor walk -- written precisely to make the affirmative
    allow-root proof sound -- and that helper re-joined the remaining tail
    VERBATIM, so a literal `..` survived into the decision. `Path.parents` treats
    `..` as an ordinary name, so the gate matched on the pre-`..` prefix while
    `mkdir(parents=True)` and the kernel resolved it for real. Measured against
    that revision: a path of the form <approved-root>/absent/../victim_checkout/
    was approved, the ancestor walk found no `.git`, and a snapshot was written
    inside the checkout with `git status` reporting it untracked. The helper
    written to close hole four opened hole five.

    Belt and braces: any `..` or `.` still present after canonicalization is a
    hard refusal rather than something to interpret.
    """
    expanded = os.path.expanduser(str(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    canonical = Path(os.path.realpath(expanded))
    if any(part in ("..", ".") for part in canonical.parts):
        raise Failure(
            f"refusing to use the HA snapshot path {path}: it still contains a "
            f"'..' or '.' component after canonicalization ({canonical}). Every "
            "location check would then be reading a different path from the one "
            "the kernel writes to."
        )
    return canonical


def path_is_under(path: Path, root: Path) -> bool:
    """Containment by PATH COMPONENTS, never by string prefix.

    `"/tmpfoo/x".startswith("/tmp")` is True and is exactly the bug this avoids;
    `Path("/tmpfoo/x").parents` contains `/tmpfoo` and `/`, not `/tmp`.
    """
    return path == root or root in path.parents


def ancestor_holding_dot_git(path: Path) -> Path | None:
    """The nearest ancestor of `path` that holds a `.git` entry, or None.

    PURE FILESYSTEM, no subprocess, and it is the PRIMARY refusal test because
    git must never be able to GRANT safety. `git rev-parse` prints the canonical
    `fatal: not a git repository` line -- while standing inside a real checkout --
    under at least six measured conditions: a linked worktree whose .git-file
    gitdir has been removed (`fatal: not a git repository: (null)`), a .git file
    whose relative gitdir is missing, a repo with .git/HEAD deleted, one with
    .git/objects deleted, a mode-000 .git, and an inherited GIT_DIR=/nonexistent
    or GIT_CEILING_DIRECTORIES=<repo>. Every one of those leaves a `.git` entry
    sitting in an ancestor, which is what this looks for.

    Unreadable is REFUSE, not accept: an OSError here (a mode-000 ancestor) means
    the question could not be answered, so the answer is "assume tracked".
    """
    for parent in path.parents:
        try:
            # lstat, not exists(): a DANGLING `.git` symlink must still count --
            # exists() follows the link and would report False for it.
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            # A path component is a FILE. Reporting that as "inside a git work
            # tree" would send the operator hunting for a checkout that does not
            # exist; name the real cause instead. Still a refusal.
            raise Failure(
                f"cannot check {path} for a git work tree: {parent} is not a "
                "directory, so the snapshot path cannot exist as written. "
                "Refusing to write personal home-state data on an unchecked path."
            ) from None
        except OSError:
            return parent
        return parent
    return None


def _git_probe_roots(
    path: Path, probe: Path, env: dict[str, str], strict: bool
) -> list[Path]:
    """Roots `git rev-parse` reports from `probe`. WIDEN-ONLY: never grants."""
    roots: list[Path] = []
    for flag in ("--show-toplevel", "--git-common-dir"):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", flag],
                cwd=str(probe), env=env, capture_output=True, text=True,
                check=False,
            )
        except OSError as exc:
            if not strict:
                return roots
            # git missing or unrunnable. The guard cannot answer, so REFUSE: a
            # privacy guard that fails open silently is the exact shape the rest
            # of this script exists to refuse, and the pre-fix static test at
            # least could not do it.
            raise Failure(
                f"cannot verify that {path} lies outside a git work tree "
                f"({type(exc).__name__}). Refusing to write personal home-state "
                "data without that check."
            ) from None
        if proc.returncode == 0 and proc.stdout.strip():
            raw = Path(proc.stdout.strip())
            # `--git-common-dir` may print a RELATIVE path ('.git'), and
            # Path('.git').resolve() resolves against the PROCESS CWD -- adding a
            # phantom forbidden root taken from wherever the operator happened to
            # run this, which then refuses legitimate scratch paths while naming a
            # directory that is not a checkout. Resolve against the probe instead.
            found = raw.resolve() if raw.is_absolute() else (probe / raw).resolve()
            # It points at `<main checkout>/.git`; its parent is the tracking tree.
            roots.append(found.parent if found.name == ".git" else found)
            continue
        if not strict:
            # The INHERITED-env probe is allowed to fail silently, and ONLY
            # because it can never be the thing that says yes: the allow-root
            # test and the strict probe below still gate every write. Its whole
            # job is to see the work tree that only the inherited environment
            # defines.
            continue
        stderr = (proc.stderr or "").strip()
        # ANCHORED to the start of git's canonical line, and it means only "git
        # contributes no extra root" -- the allow-root test and the filesystem
        # test in the caller have already decided the safety question. The old
        # substring form also accepted "no such file or directory", which is what
        # a dangling worktree gitdir prints from INSIDE a tracked tree.
        if stderr.startswith("fatal: not a git repository"):
            continue
        raise Failure(
            f"cannot verify that {path} lies outside a git work tree: "
            f"`git rev-parse {flag}` exited {proc.returncode}. Refusing to write "
            "personal home-state data without that check."
        )
    return roots


def git_worktrees_containing(path: Path) -> list[Path]:
    """EXTRA git work trees that would track `path`, on top of the filesystem test.

    This runs only to WIDEN the forbidden set (a sibling-worktree layout points
    its `.git` file at a common dir elsewhere, and the toplevel may differ from
    the nearest `.git`-holding ancestor). It can never shrink it: the caller has
    already required the path to sit under an approved root and refused every
    path with a `.git` in an ancestor before asking git anything, so a git answer
    of "not a repository" means only "git adds no extra roots" -- never "safe".

    Testing containment against the SCRIPT's repo root is not enough here: this
    repo drives its agents from worktrees under `.claude/worktrees/<id>/`, which
    are subdirectories of the main checkout. A snapshot written to the main
    checkout's root is outside the worktree, passes a root-relative test, and
    lands in a tracked tree anyway. Ask git about the destination too.

    TWO probes, and neither is redundant -- do not delete one.
      * SANITIZED env: an inherited GIT_DIR / GIT_CEILING_DIRECTORIES makes real
        git DENY a real repository, so a probe that inherits them can be blinded
        into reporting "not a git repository" from inside a tracked tree. Strict:
        any failure other than the canonical line refuses.
      * INHERITED env: an EXTRINSICALLY defined work tree (bare repo plus
        exported GIT_DIR/GIT_WORK_TREE -- dotfiles, hooks, `rebase --exec`,
        `bisect run`, `submodule foreach`) exists ONLY in that environment. There
        is no `.git` in the destination's ancestry to find, and sanitizing the
        env deletes the only evidence. An earlier revision detected this shape by
        accident and a later "cleanup" removed the detection; this probe restores
        it deliberately.

    RESIDUALS, named so nobody mistakes this for exhaustive: (a) `core.worktree`
    set in a bare repo's config, pointing INTO an allow-root, is not detected
    here; (b) PATH or GIT_CONFIG_* manipulation can suppress the widening. The
    inversion is what makes those non-deciding: an unanticipated shape has to get
    past the ALLOW-ROOT test first, and an unlisted root is a refusal by default.
    """
    # Walk UP to the nearest EXISTING directory. `git -C <nonexistent>` exits 128,
    # which used to make this return [] and quietly degrade the guard to the
    # script's own root; `mkdir(parents=True)` then CREATED the missing directory
    # inside the tracked tree and wrote a 590 KB home-state readout into it.
    # Measured before this fix: /Users/surge/dev/homelab/scratch171/ha.json was
    # accepted, and `git status` in the main checkout reported it untracked.
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    sanitized = {k: v for k, v in os.environ.items() if k not in GIT_ENV_STRIP}
    sanitized["LC_ALL"] = "C"
    inherited = dict(os.environ)
    inherited["LC_ALL"] = "C"

    roots = _git_probe_roots(path, probe, sanitized, strict=True)
    for root in _git_probe_roots(path, probe, inherited, strict=False):
        if root not in roots:
            roots.append(root)
    return roots


def load_or_capture_ha_states(
    ha_url: str,
    token: Secret,
    snapshot: Path | None,
    repo_root: Path,
    approved: tuple[list[Path], list[str]],
) -> tuple[list[dict], float]:
    """HA states, optionally pinned to a snapshot file so a re-run is reproducible.

    Provenance goes to STDERR, never stdout: a "captured" vs "replayed" line on
    stdout would make the capture run and the replay run differ, which is exactly
    the property the snapshot exists to establish.
    """
    if snapshot is None:
        return fetch_ha_states(ha_url, token), time.time()

    # RESOLVE FIRST, then decide. Everything below judges the real path, so a
    # symlink sitting in an approved root and pointing into a checkout is refused
    # for where it LANDS. (TOCTOU residual, accepted for this single-operator
    # threat model: the checks run on the resolved path and O_EXCL protects only
    # the final component, so a parent-directory symlink swapped between the check
    # and the open is not covered.)
    approved_roots, dropped_roots = approved
    snapshot = canonical_path(snapshot)
    # The .gitignore backstop matches `ha-states*.json` by basename, so a snapshot
    # called anything else is NOT covered by it. Enforce the name the backstop
    # knows, so the two halves of the defence line up instead of only appearing to.
    if not snapshot.name.startswith("ha-states") or snapshot.suffix != ".json":
        raise Failure(
            f"HA snapshot must be named ha-states*.json (got {snapshot.name}); "
            "that is the pattern .gitignore covers as a second line of defence"
        )
    # /api/states is a full readout of a home: occupancy, device names, presence.
    # It must never land in a tracked tree, where a stray `git add -A` publishes it.
    #
    # AFFIRMATIVE ALLOW, checked FIRST: the path must lie under a root the
    # operator approved. Four rounds of enumerating forbidden shapes each missed
    # the next one; an unlisted root is a refusal, so an unanticipated layout
    # fails closed instead of open. See allow_roots().
    if not any(path_is_under(snapshot, root) for root in approved_roots):
        detail = ""
        if dropped_roots:
            # Naming the DROPPED candidates matters: an operator who passed
            # --snapshot-allow-root and still got refused would otherwise have no
            # way to see that their root was discarded, or why.
            detail = " Candidate roots dropped: " + "; ".join(dropped_roots) + "."
        raise Failure(
            f"refusing to write an HA snapshot at {snapshot}: it is not under any "
            f"approved root ({', '.join(str(r) for r in approved_roots)})."
            f"{detail} /api/states carries personal home-state data, so this guard "
            "allows only paths that are positively approved -- pass "
            "--snapshot-allow-root DIR to add one."
        )
    # Then every earlier layer, unchanged. Pure filesystem next: any ancestor
    # holding a `.git` entry is a refusal, readable or not. Git is asked only
    # afterwards, and only to ADD roots -- four review rounds found a way to make
    # `git rev-parse` report "not a git repository" from inside a tracked tree, so
    # the subprocess is not allowed to be the thing that says yes.
    tracked_ancestor = ancestor_holding_dot_git(snapshot)
    if tracked_ancestor is not None:
        raise Failure(
            f"refusing to write an HA snapshot inside a git work tree "
            f"({tracked_ancestor}): /api/states carries personal home-state data. "
            "Put it in a scratch directory outside every checkout."
        )
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
        except OSError as exc:
            raise Failure(
                f"cannot read HA snapshot {snapshot}: {exc.strerror}"
            ) from None
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
            raise Failure(
                f"HA snapshot {snapshot} has no usable captured_at stamp "
                "(missing or non-numeric)"
            )
        try:
            # A JSON integer has no size limit, so `10**400` arrives as a Python
            # int and float() raises OverflowError -- an uncaught traceback where
            # every other malformed-payload case is a named Failure.
            captured_float = float(captured_at)
        except OverflowError:
            raise Failure(
                f"HA snapshot {snapshot} has a captured_at stamp too large to "
                "represent as a float -- refusing to read it as a timestamp"
            ) from None
        if not math.isfinite(captured_float):
            # json.loads accepts a BARE NaN, and NaN switches the freshness check
            # off rather than failing it: `nan > tolerance` is False, so a
            # snapshot captured at any distance from `end` would sail through and
            # the run would exit 0 on a different instance's entity list.
            raise Failure(
                f"HA snapshot {snapshot} has no usable captured_at stamp "
                "(not finite)"
            )
        # A snapshot taken far from `end` describes a different instance than the
        # VictoriaMetrics window does: entities created, renamed or deleted in
        # between land in never-seen and vm_only as pure artefacts. A grossly
        # stale one happens to empty G3's control set, but that backstop is
        # accidental and does not cover the near-stale regime.
        print(f"HA states REPLAYED from snapshot {snapshot} ({len(states)} "
              f"entities, captured_at {captured_at})", file=sys.stderr)
        return states, captured_float

    states = fetch_ha_states(ha_url, token)
    try:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Failure(
            f"cannot create {snapshot.parent}: {exc.strerror}"
        ) from None
    # Written 0600: see the personal-data note above.
    captured_at = int(time.time())
    try:
        fd = os.open(str(snapshot), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"captured_at": captured_at, "states": states}, handle)
    except OSError as exc:
        raise Failure(f"cannot write HA snapshot {snapshot}: {exc.strerror}") from None
    print(f"HA states CAPTURED to snapshot {snapshot} ({len(states)} entities)",
          file=sys.stderr)
    return states, float(captured_at)


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


def fetch_vm_counted(
    vm_url: str, auth: Secret, start: int, end: int
) -> tuple[set[str], int]:
    """The seen set derived a SECOND way, via count_over_time. Returns (set, drops).

    Same endpoint and window as tlast_over_time but a different aggregation, so
    the two can genuinely disagree -- which is what makes P3 falsifiable where P1
    and P2 are true by construction.

    The drop counter exists because this leg USED to skip malformed rows silently
    while its twin counted them -- and its twin also drops rows whose VALUE is
    unparseable or non-finite, which this leg never even reads. A row dropped from
    one leg and kept by the other is a P3 disagreement caused by a malformed row,
    not by "the query layer", which is what P3 blamed by name. Both counters are
    printed in the counts table and named in P3's failure text.
    """
    window = end - start
    doc = vm_post(
        vm_url,
        "/api/v1/query",
        [
            (
                "query",
                f'count(count_over_time({{db="ha"}}[{window}s])) '
                "by (domain, entity_id)",
            ),
            ("time", str(end)),
        ],
        auth,
    )
    out: set[str] = set()
    dropped = 0
    for item in vm_result(doc, "/api/v1/query"):
        metric = item.get("metric") if isinstance(item, dict) else None
        if not isinstance(metric, dict):
            dropped += 1
            continue
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if domain and entity:
            out.add(f"{domain}.{entity}")
        else:
            dropped += 1
    return out, dropped


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
        metric = item.get("metric") if isinstance(item, dict) else None
        if not isinstance(metric, dict):
            dropped += 1
            continue
        domain, entity = metric.get("domain"), metric.get("entity_id")
        if not domain or not entity:
            dropped += 1
            continue
        try:
            ts = float(item["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            dropped += 1
            continue
        # NaN AND +/-Inf: `end - inf` is -inf and fmt_silence's int() would raise
        # OverflowError. The previous comment named NaN and left inf open.
        if not math.isfinite(ts):
            dropped += 1
            continue
        # A sample timestamp well AFTER the window end is instrument breakage,
        # not data. 60s of slack absorbs VictoriaMetrics' own rounding; anything
        # beyond it is refused, the same way every other malformed payload here
        # is refused. Note what this does NOT do: a value INSIDE the slack still
        # yields a negative `end - ts`, so the render is clamped at zero in
        # fmt_silence -- the refusal bounds the damage, the clamp handles the
        # tolerated remainder.
        if ts > end + 60:
            raise Failure(
                f"VM POST /api/v1/query: tlast_over_time returned {ts:.0f} for "
                f"{domain}.{entity}, which is after --end ({end}) by more than "
                "60s -- silence would render negative. Refusing to read a broken "
                "instrument."
            )
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
                        help="window end, epoch seconds (default: now). A PAST end "
                             "requires --ha-states-json pointing at a snapshot "
                             "captured at that time: the HA readout is checked "
                             "against `end` on every path, and a live fetch can "
                             "only describe now.")
    parser.add_argument("--vm-url", default=DEFAULT_VM_URL)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--ha-states-json", default=None,
                        help="pin the HA side: capture /api/states here on first use, "
                             "replay it afterwards. Required to make stdout "
                             "byte-identical across runs, because HA is a live source. "
                             "It holds home-state data, so the path must lie under an "
                             "APPROVED ROOT: $TMPDIR, /tmp, /private/tmp, or a "
                             "directory named with --snapshot-allow-root. Anything "
                             "else is refused.")
    parser.add_argument("--snapshot-allow-root", action="append", default=None,
                        metavar="DIR",
                        help="add a directory an HA snapshot may be written under "
                             "(repeatable). The guard is affirmative-allow: a path "
                             "outside every approved root is refused, whether or not "
                             "it looks like a checkout. Must be ABSOLUTE, and may "
                             "not be '/' -- a relative root would resolve against "
                             "the current directory and '/' would approve the whole "
                             "machine. A dropped root is named in the refusal.")
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
    # The only wallclock read that REACHES THE REPORT: with --start/--end pinned,
    # every printed number is a function of (start, end) and of the series-index
    # state (see the docstring). It is not the only time.time() in the script --
    # the live-fetch and capture paths stamp the HA readout with the clock too --
    # so this comment scopes the claim rather than making it absolute.
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
    fully_pinned = (bool(args.ha_states_json) and args.start is not None
                    and args.end is not None)
    if fully_pinned:
        emit("           HA input PINNED to a snapshot and the window pinned "
             "explicitly -- byte-identical on a re-run against an")
        emit("           unchanged series index. /api/v1/series is per-UTC-day, "
             "so the trailing day's bucket grows until it closes;")
        emit("           the query legs (tlast/count_over_time) are pure "
             "functions of the window. Run promptly after `end`.")
    elif args.ha_states_json:
        emit("           HA input pinned, WINDOW IS NOT: --start/--end were not "
             "both given, so `end` moved with the clock and")
        emit("           every silence figure moves with it. Pin BOTH to "
             "reproduce byte-for-byte -- and even then only against")
        emit("           an unchanged series index, which is per-UTC-day and "
             "grows until the trailing day closes.")
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
    emit(f"VM       : {args.vm_url}  (series + tlast_over_time + "
         "count_over_time, read-only)")
    emit()

    # -- HA side -------------------------------------------------------------
    states, ha_as_of = load_or_capture_ha_states(
        args.ha_url,
        ha_token,
        Path(args.ha_states_json) if args.ha_states_json else None,
        repo_root,
        allow_roots(args.snapshot_allow_root),
    )
    del ha_token

    # Freshness is checked on EVERY path, not only on replay. Checking it only
    # when replaying guarded the byte-identical REPEAT of a run while letting the
    # run that actually PRODUCES the dead list through -- measured: identical 1h
    # staleness passed live and passed on capture, and was refused only on replay.
    # A guard that fires on the healthy repeat and never on the original finding
    # is inverted. An HA readout far from `end` describes a different instance
    # than the VictoriaMetrics window does, and the difference lands in never-seen
    # and vm_only as pure artefacts.
    ha_skew = abs(ha_as_of - end)
    if ha_skew > HA_FRESHNESS_TOLERANCE_S:
        raise Failure(
            f"the HA entity list is {ha_skew:.0f}s away from --end (tolerance "
            f"{HA_FRESHNESS_TOLERANCE_S}s), so it describes a different instance "
            "than the window does. For a PAST --end, replay a snapshot captured "
            "at that time (--ha-states-json): a fresh capture can only describe "
            "now, so re-capturing cannot fix a historical window. For a window "
            "ending now, pin --end to the capture time."
        )

    ha: dict[str, dict] = {}
    dropped_no_dot = 0
    states_malformed = 0
    duplicate_entity_ids = 0
    unparseable_last_changed = 0
    for item in states:
        if not isinstance(item, dict):
            # /api/states is a list of objects; a row that is not one raised
            # AttributeError on `.get` and left a raw traceback. Counted like its
            # neighbours instead, and reconciled against len(states) below.
            states_malformed += 1
            continue
        full_id = item.get("entity_id", "")
        if not isinstance(full_id, str) or "." not in full_id:
            dropped_no_dot += 1
            continue
        if full_id in ha:
            # A duplicate entity_id used to vanish into dict last-wins, so
            # len(states) and len(ha) disagreed with nothing to say why.
            duplicate_entity_ids += 1
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
    malformed_series = 0
    for metric in series:
        if not isinstance(metric, dict):
            # Same shape as the HA-side row guard: a non-object series entry
            # raised AttributeError here rather than being counted.
            malformed_series += 1
            continue
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
    # Fetched next to its twin so BOTH drop counters can be printed in the counts
    # table below, and so the two queries sit as close together in time as
    # possible (see the N8 timing note beside P3).
    counted_seen, counted_dropped = fetch_vm_counted(args.vm_url, vm_auth, start, end)

    # The two VM endpoints disagree in BOTH directions, for different reasons:
    #   index_only -- /api/v1/series resolves against a per-DAY inverted index, so
    #                 it can list a series whose samples fall outside [start,end];
    #                 or a sample landed so recently it is not queryable yet
    #                 (measured: present in /api/v1/export at T, invisible to a
    #                 query at T+5s). This is also why the index leg is not a pure
    #                 function of the window (see the docstring): a window ending
    #                 mid-day picks up that day's whole bucket, which grows until
    #                 the day closes. Measured on one pinned window re-run 3.9h
    #                 later: index_only 0 -> 55, vm_only 0 -> 46, both query legs
    #                 steady at 245. The union keeps that drift in the SAFE
    #                 direction -- everything it adds is evidence of life.
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
    emit(f"  states rows that are not objects    {states_malformed:5d}   dropped")
    emit(f"  duplicate entity_id (last wins)     {duplicate_entity_ids:5d}"
         f"   {len(states)} rows = {len(ha)} unique + "
         f"{dropped_no_dot + states_malformed} dropped + {duplicate_entity_ids} dup")
    emit(f"  last_changed unparseable            {unparseable_last_changed:5d}"
         "   G3/restore-burst read past these")
    emit(f"VM series over window                 {len(series):5d}"
         f"   ({len(metric_names)} distinct metric names)")
    emit(f"  series rows that are not objects    {malformed_series:5d}   dropped")
    emit(f"  series with no __name__             {nameless_series:5d}")
    emit(f"  series lacking domain or entity_id  {len(unjoinable):5d}   (cannot join)")
    for name in sorted(set(unjoinable))[:10]:
        emit(f"      {name}")
    emit(f"  tlast rows dropped (labels/value)   {tlast_dropped:5d}")
    emit(f"  count_over_time rows dropped        {counted_dropped:5d}"
         "   neither drop can create a dead entity: see P3")
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
    emit("   construction the FRESHEST entities), and NOTHING BELOW COVERS IT "
         "EITHER: P3 re-derives the seen set")
    emit("   from the SAME endpoint and the same selector with a different "
         "aggregation, not from a second source.")
    emit("   The index/query split is printed above as counts (index_only / "
         "query_only) and is guarded by nothing;")
    emit("   taking their union is the conservative direction, which is what "
         "bounds the damage, not a check.")
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

    # P2 -- ALSO a source-code canary, not a data check, and it was briefly
    # mislabelled as "the one that CAN fail on data". Since the seen set is the
    # union, `x in last_sample` implies `x in vm_seen` implies `x not in
    # never_seen`: `leaked` is empty for every possible input. The fixture that
    # fails it does so by reverting the union at its definition -- a source edit,
    # exactly like P1. Worth keeping for that; not evidence about the data.
    leaked = sorted(set(last_sample) & set(never_seen))
    p2_ok = not leaked
    emit(f"P2 union-code canary          {'PASS' if p2_ok else 'FAIL'}"
         f"   {len(leaked)} dated entities in the never-seen list")
    emit("   (structural: true for all inputs by construction -- fails only if the "
         "seen set stops reading both endpoints)")
    for entity in leaked[:10]:
        emit(f"   LEAKED: {entity} last sample {iso(last_sample[entity])}")
    if not p2_ok:
        failures.append(
            f"P2: {len(leaked)} entities are in the dead list AND have an in-window "
            "sample timestamp -- the seen set is not reading both endpoints"
        )

    # P3 -- the falsifiable one. It derives the seen set a SECOND time from a
    # different MetricsQL aggregation over the same window, so nothing about it is
    # implied by the union and a real disagreement between the two shows up as a
    # failure rather than as a quietly smaller dead list. Pre-tested on this
    # instance: 245 vs 245, zero disagreement.
    #
    # TIMING (documented, deliberately NOT compensated): the two queries run
    # seconds apart, and a sample landing near `end` can become queryable in
    # between -- measured elsewhere in this repo at ~45s from ingestion to
    # queryable. That makes a rare SPURIOUS P3 failure possible. It fails in the
    # safe direction (an instrument-broken verdict, no dead list published), and
    # an end-lag would trade a rare loud false alarm for a permanent silent blind
    # spot at the fresh edge, so the gap is documented rather than papered over.
    #
    # WHAT THE DROP COUNTERS DO AND DO NOT BOUND. An earlier version of this
    # comment named the wrong enforcer: it claimed a labelled row dropped from the
    # tlast leg is still carried by the /api/v1/series index leg of the union.
    # That is not guaranteed -- this script prints a `query_only` category
    # precisely because the index does not always list a pair the query dates.
    # The actual enforcer is the OTHER QUERY LEG: count_over_time still returns
    # that series, so a value-dropped row shows up as a P3 DISAGREEMENT and the
    # run refuses to publish a dead list at all. A row with no usable labels never
    # identified an entity in the first place; its index twin is counted in
    # `unjoinable`. So the counters ATTRIBUTE a P3 failure, and P3 is what bounds
    # the finding.
    tlast_set = set(last_sample)
    only_counted = sorted(counted_seen - tlast_set)
    only_tlast = sorted(tlast_set - counted_seen)
    p3_ok = not only_counted and not only_tlast
    emit(f"P3 two aggregations agree     {'PASS' if p3_ok else 'FAIL'}"
         f"   count_over_time {len(counted_seen)} vs tlast_over_time "
         f"{len(tlast_set)}; {len(only_counted) + len(only_tlast)} disagree")
    for entity in (only_counted + only_tlast)[:10]:
        emit(f"   DISAGREEMENT: {entity}")
    if not p3_ok:
        failures.append(
            f"P3: count_over_time and tlast_over_time disagree on "
            f"{len(only_counted) + len(only_tlast)} entities over the same window. "
            f"Rows dropped this run: count_over_time {counted_dropped}, tlast "
            f"{tlast_dropped}. A non-zero drop on EITHER leg explains the "
            "disagreement without implicating the query layer -- only if BOTH are "
            "zero does this mean the query layer is not returning a stable seen "
            "set"
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
    elif not g4_ok:
        failures.append(
            f"G4: domain=update series {g4_domain_hits}, update.* metric names "
            f"{g4_name_hits} -- the by-design subtraction is INVALID and is itself "
            "a finding"
        )
        for name in update_prefixed_names[:10]:
            emit(f"   update-prefixed name: {name}")
    emit()

    emit(f"G5 determinism precondition   {'PASS' if fully_pinned else 'N/A '}"
         f"   {'snapshot AND explicit --start/--end' if fully_pinned else 'NOT fully pinned'}"
         " -- byte-identity needs BOTH")
    emit("   (not a failure: an unpinned run is still valid, it just will not "
         "reproduce byte-for-byte)")
    emit("   Both pinned is NECESSARY, not sufficient: the series index is "
         "per-UTC-day and its trailing bucket grows")
    emit("   until that day closes, so byte-identity holds against an UNCHANGED "
         "index state. The two query legs are pure")
    emit("   functions of the window; the DEAD LIST IS NOT, because never-seen = "
         "writable - vm_seen and vm_seen is the")
    emit("   union, index leg included. The direction is still safe while the data "
         "is RETAINED: a growing index only")
    emit("   shrinks the candidate list. (Retention is the qualifier -- if samples "
         "age out or a series is deleted,")
    emit("   vm_seen SHRINKS and the list would grow.)")
    # Right by condition, and the condition is printed above so a reader can check
    # it. "query-seen" here means the TLAST leg specifically -- index_only is
    # defined as index_seen - set(last_sample), so index_only == 0 says vm_seen
    # equals the tlast set, and nothing about count_over_time. What makes the
    # statement true of BOTH query legs is P3, which passed above: it asserts the
    # two aggregations returned the same set, so with P3 green the tlast set and
    # the count_over_time set are one set.
    if index_only:
        emit(f"   THIS run's candidate list is NOT window-pure: index_only reads "
             f"{len(index_only)}, so vm_seen exceeds the tlast-seen set and the")
        emit("   list is SMALLER than a query-only reading would give -- the safe "
             "direction, but not purity.")
    else:
        emit("   THIS run's candidate list IS window-pure: index_only reads 0, so "
             "writable - vm_seen equals writable")
        emit("   - tlast-seen exactly -- and P3 passed above, so the two query "
             "legs are the same set.")
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
    emit("   query at T+5s. The per-day cause has a MEASURED magnitude, not a "
         "theoretical one: re-running one")
    emit("   pinned 28.1h window 3.9h later moved this list from 0 to 9 and "
         "index_only from 0 to 55, purely")
    emit("   because the trailing UTC day's bucket had grown. All 9 turned out to "
         "be live-after-window -- first")
    emit("   sample 21-79 min AFTER `end` -- which is the fresher cause, and the "
         "conservative direction. Full")
    emit("   list, never truncated:")
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
    string_by_domain: dict[str, list[str]] = {}
    for entity in never_seen_string:
        string_by_domain.setdefault(ha[entity]["domain"], []).append(entity)
    emit("   Enumerated BY NAME below, grouped by domain and unranked -- this is "
         "the one suspicion bucket the")
    emit("   report used to summarise as per-domain counts only, while its own "
         "text said the refutation RAISES")
    emit("   suspicion of it. Naming them is not convicting them: every line here "
         "is INCONCLUSIVE, because a")
    emit("   string-only state is exactly the case this instrument cannot date.")
    for domain in sorted(string_by_domain):
        entities = sorted(string_by_domain[domain])
        emit(f"   never-seen, string-only  {domain:<16} {len(entities):4d}"
             "   INCONCLUSIVE")
        for entity in entities:
            emit(f"      {entity}  state={ha[entity]['state']!r}")
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
    select_vm_series = sum(
        1 for m in series if isinstance(m, dict) and m.get("domain") == "select"
    )
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
    def device_matches(pool, device: str) -> list[str]:
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
        matches = device_matches(ha, device)
        emit(f"   {days:>5}d  {device}  (last_seen: {since})")
        if not matches:
            # Ask the side that can still answer: an entity deleted from HA may
            # well have samples in VictoriaMetrics, which is the one question
            # this script is uniquely able to settle.
            vm_matches = device_matches(vm_seen, device)
            dated_matches = [e for e in vm_matches if e in last_sample]
            undated_matches = [e for e in vm_matches if e not in last_sample]
            # "holds in-window samples" may only be claimed for a DATED pair. An
            # index-only listing is not a sample timestamp: the per-day index can
            # name a series whose samples fall outside the window entirely.
            if dated_matches:
                emit("            VERDICT: ABSENT-FROM-HA, PRESENT IN VM -- deleted "
                     "from HA, yet VictoriaMetrics holds in-window samples:")
                for entity in dated_matches:
                    quiet = fmt_silence(end - last_sample[entity])
                    emit(f"                     {entity}  silence={quiet}")
                for entity in undated_matches:
                    emit(f"                     {entity}  index-listed, undated")
            elif undated_matches:
                emit("            VERDICT: ABSENT-FROM-HA, INDEX-LISTED (undated) "
                     "-- deleted from HA; the per-day series index names it but no")
                emit("                     in-window sample timestamp exists, so "
                     "this is NOT evidence of an in-window sample:")
                for entity in undated_matches:
                    emit(f"                     {entity}  index-listed, undated")
            else:
                emit("            VERDICT: ABSENT-FROM-HA -- no entity's object_id "
                     "matches this name in HA or in VM (deleted)")
            continue
        # DISAGREE is this report's most consequential verdict, so it is gated on
        # a DATED in-window sample, never on membership of vm_seen. vm_seen is the
        # union and includes the per-day index leg, which grows all day: a re-run
        # made hours later could otherwise MANUFACTURE a DISAGREE out of an
        # after-window index listing, while the verdict text claims "a sample
        # inside the window". An index-only hit gets its own, weaker verdict.
        any_dated = False
        any_index_only = False
        for entity in matches:
            if entity in excluded_update:
                category = "excluded(update)"
            elif entity in absent_by_design:
                category = "absent-by-design"
            else:
                category = "writable"
            if entity in last_sample:
                any_dated = True
                vm_status = (
                    f"VM-SEEN(dated) silence="
                    f"{fmt_silence(end - last_sample[entity])}"
                )
            elif entity in vm_seen:
                any_index_only = True
                vm_status = "INDEX-LISTED (undated)"
            else:
                vm_status = "never-seen"
            emit(f"            {entity}  [{category}]  {vm_status}  "
                 f"state={ha[entity]['state']!r}")
        if any_dated:
            emit("            VERDICT: DISAGREE -- VictoriaMetrics holds a sample "
                 f"inside the {window / 3600:.1f}h window for a device `last_seen` "
                 "calls dead")
        elif any_index_only:
            emit("            VERDICT: INDEX-LISTED (undated) -- the per-day series "
                 "index names a matching series but no in-window")
            emit("                     sample timestamp exists; the sample may fall "
                 "outside the window entirely. NOT counted as DISAGREE.")
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

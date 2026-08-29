#!/bin/sh
# roles/telegraf_agent (#194) — RAPL package power, in WATTS, for both physical
# Proxmox hosts. Emits influx line protocol on stdout:
#
#   rapl,domain=package-0 power_watts=2.671234,energy_uj=81017680655
#
# -> prometheusremotewrite names those `rapl_power_watts` and `rapl_energy_uj`,
# tag `domain`; the agent stamps `host` (telegraf.conf's
# `hostname = "<inventory_hostname>"`).
#
# THE TWO FIELDS ARE SEPARATED BY A COMMA, NOT A SPACE, and that is not a style
# choice: in influx line protocol the first SPACE after the field set begins the
# optional TIMESTAMP. `power_watts=2.6 energy_uj=81017680655` therefore does not
# emit two fields — it emits one field with an 81-second-past-the-epoch
# timestamp, which parses cleanly and lands the sample in 1970. Keep the comma.
#
# WHY THE RAW COUNTER IS EXPORTED BESIDE THE DERIVED RATE (#206). The wattage
# DESTROYS the freshness information: a counter the firmware has stopped updating
# is still readable, so this script computes a delta of zero and prints a
# faithful, confident `power_watts=0.000000` forever. That is indistinguishable
# from a genuinely idle domain in the value domain — eq12 `uncore` prints exactly
# that for 268 of every 1440 samples (measured over 24 h, 2026-08-29) — so no
# threshold on watts can tell frozen from idle for such a domain. The raw
# monotonic counter can: over 60 s at idle eq12 `uncore` advanced 488 uJ while
# its 1 s wattage rounded to zero (measured 2026-08-29, with `package-0`
# advancing 355753179 uJ in the same pair of reads as the positive control).
# `changes(rapl_energy_uj{domain="uncore"}[30m])` is therefore a real freshness
# question anyone can ask of ANY domain, without an ssh session.
#
# It is the RAW SECOND-PASS read, not a delta, and it WRAPS at this zone's
# `max_energy_range_uj` (sawtooth). It is a freshness observable, never a rate
# source — `rate()`/`increase()` over it is exactly the negative-spike trap this
# script exists to avoid, which is why the watts field stays the value anything
# numeric reads.
#
# `%.0f` AND NEVER `%d`: mawk 1.3.4 truncates %d at INT_MAX (2147483647) and
# these counters run to 2.6e11 — eq12 `core` read 255107786802 on 2026-08-29, two
# orders of magnitude past the cliff, so %d would print a silently wrong number
# rather than fail. Doubles are exact to 2^53, so %.0f is lossless here.
#
# The two fields ride ONE printf, so a zone emits BOTH or NEITHER. That coupling
# is load-bearing: it keeps the domain SET identical across the two families, so
# the existing `obs-rapl-power-absent-*` rules and the deploy-time domain-set
# proof own `rapl_energy_uj`'s delivery too, rather than the new family needing
# its own absence owner (#189 ownership stated once, not duplicated).
#
# WHY A SCRIPT AND NOT [[inputs.intel_powerstat]] — both halves MEASURED
# 2026-08-25 with `telegraf --test` under the real unit user and capabilities:
#   * n5pro: the plugin refuses to initialise at all — "failed to initialize
#     metric fetcher interface: host processor is not supported". It cannot
#     serve the AMD box, full stop.
#   * eq12: it DOES run, and its first gather emits
#     `current_power_consumption_watts=826963607.79` — an absurd first-sample
#     artefact (no prior read to delta against), i.e. a garbage spike at every
#     agent restart even where the plugin is supported.
# A per-host split (plugin on eq12, script on n5pro) would then export two metric
# names for one signal — the coretemp/k10temp divergence trap one layer up. One
# script on both hosts instead. Do not "simplify" this back to the plugin.
#
# WATTS AT THE SOURCE, because these are FAST-WRAPPING counters:
# `max_energy_range_uj` is 262143328850 uJ on eq12 but only 65532610987 uJ
# (65.5 kJ) on n5pro — a rollover roughly every 3 h at idle and ~20 min under
# load. A `rate()`/`increase()` over the raw counter emits a large negative
# spike at every wrap. This script takes two reads ~1 s apart, wrap-corrects a
# negative delta by adding that zone's own `max_energy_range_uj`, and divides by
# the ACTUAL elapsed time rather than an assumed 1.0 s. Over a ~1 s window a
# DOUBLE wrap is physically impossible (it would need >65 kW on n5pro), so one
# correction is the only one that can be needed.
#
# But the correction is still a GUESS about what a negative delta MEANT, and a
# counter RESET produces the same sign. It is therefore bounded by physics: a
# corrected value above RAPL_MAX_PLAUSIBLE_WATTS (default 1000) is discarded with
# a message on stderr rather than exported. See the END block for the measured
# reset that motivated this.
#
# WHAT THE SAMPLE MEANS, stated honestly because the dashboards depend on it: it
# is a ~1 s INSTANTANEOUS snapshot taken once per 60 s collection interval, NOT a
# 60 s average. Keep the agent interval at 60 s (README records why widening it
# is worse, not better).
#
# THE DOMAIN LABEL COMES FROM THE SIBLING `name` FILE, never from the sysfs node
# index: `intel-rapl:0` -> `package-0`, `intel-rapl:0:0` -> `core`. Indices are
# not stable across boots or hardware.
#
# Gauge semantics per CLAUDE.md: a zone that cannot be read completely emits
# NOTHING — never a fabricated zero. That is why the deploy asserts an EXACT
# domain set per host rather than "at least one": partial loss thins the series
# silently, so the count is the assertion.
#
# RAPL IS PACKAGE POWER, NOT WALL POWER. It excludes NVMe, fans, PSU conversion
# loss, and on n5pro the five drive bays and the whole NAS side of the box.
# Nothing downstream may present it as a machine total.
#
# Arguments: domain names to EXCLUDE, one per argument (templated from
# `telegraf_agent_rapl_exclude_domains`). n5pro passes `core` — its powercap
# `core` subdomain was measured to be ONE core of twelve, not a package
# aggregate — established by LOAD PLACEMENT, not by its magnitude; see the
# role README and docs/n5pro.md for both hosts' measured tables.
#
# RAPL_SYSFS_ROOT overrides the sysfs root (default /sys/class/powercap) so the
# wrap branch can be exercised against a stub tree off-host. It must not contain
# whitespace — zone paths are word-split below.
set -u

root="${RAPL_SYSFS_ROOT:-/sys/class/powercap}"

# The plausibility ceiling, in watts, validated HERE rather than trusted. awk
# would take a non-numeric -v value as a STRING, and `watts > ceiling` would then
# be a string comparison ("2.32" > "abc" is false), so every implausible reading
# would sail through: the guard would fail OPEN, which is the wrong direction for
# a guard (CLAUDE.md). Only reachable through the debug override, but a guard
# that silently stops guarding is exactly the failure class this file is about.
# No '' arm: `:-` substitutes the default for unset AND null, so $ceiling is
# never empty here and such an arm could never fire — the same unfalsifiable
# shape this file just deleted from the wrap branch. '.' IS reachable (one dot,
# no other characters) and is rejected by name; without it awk would take "." as
# a string, every comparison would be a string compare, and the script would
# discard everything and exit 1 — failing closed, but with a baffling message.
ceiling="${RAPL_MAX_PLAUSIBLE_WATTS:-1000}"
case "$ceiling" in
  '.' | *[!0-9.]* | *.*.*)
    echo "rapl_power.sh: RAPL_MAX_PLAUSIBLE_WATTS=$ceiling must be a plain decimal number (no exponent, no sign)" >&2
    exit 1
    ;;
esac

# Space-delimited on both sides so a match is a WHOLE name: excluding `core`
# must not also exclude a future `core-1`.
excluded=" $* "

nl='
'

# Discovery pass. `intel-rapl:*` deliberately excludes the bare `intel-rapl`
# control-type directory, which has no energy_uj. It ALSO — and this is not an
# accident to be tidied away — excludes `intel-rapl-mmio:*`, whose zones are
# named `package-0` too on some Intel platforms. Widening this glob to
# `intel-rapl*` would give one `domain=package-0` series two writers, which
# presents as an unstable value nobody can trace. Do not generalise it.
# A zone is carried forward only if EVERY field it needs is readable and
# well-formed; anything else is dropped here rather than half-reported later.
meta=""
for zone in "$root"/intel-rapl:*; do
  [ -r "$zone/energy_uj" ] || continue
  name=$(cat "$zone/name" 2>/dev/null) || continue
  # Reject anything that would break the space-separated hand-off to awk below,
  # or the influx tag value: real RAPL names are package-0, core, uncore, psys.
  case "$name" in '' | *[!A-Za-z0-9_.:-]*) continue ;; esac
  case "$excluded" in *" $name "*) continue ;; esac
  max=$(cat "$zone/max_energy_range_uj" 2>/dev/null) || continue
  case "$max" in '' | *[!0-9]*) continue ;; esac
  meta="$meta$zone $name $max$nl"
done

# Zero zones with the input CONFIGURED is a real fault, not a quiet no-op: the
# exec block is only templated where telegraf_agent_rapl_power is true. Fail
# loudly so telegraf logs it, rather than exporting silence (which the deploy's
# domain-set assert and the absence rules would then have to catch alone).
if [ -z "$meta" ]; then
  echo "rapl_power.sh: no readable RAPL zones under $root" >&2
  exit 1
fi

# Pathname expansion off from here down. The loops below word-split $zones
# deliberately, and word-splitting is all they may do — a glob character
# reaching a zone path must not silently become a different path.
set -f

zones=$(printf '%s' "$meta" | cut -d' ' -f1)

# Two timestamped read passes. The timestamps BRACKET each pass and the elapsed
# time is measured between their midpoints, so the interval used is the one that
# actually separated the two readings of a given zone — not an assumed 1.0 s,
# and not skewed by however long the pass itself took.
ta=$(date +%s%N)
first=$(for zone in $zones; do printf '%s %s\n' "$zone" "$(cat "$zone/energy_uj" 2>/dev/null)"; done)
tb=$(date +%s%N)
sleep 1
tc=$(date +%s%N)
second=$(for zone in $zones; do printf '%s %s\n' "$zone" "$(cat "$zone/energy_uj" 2>/dev/null)"; done)
td=$(date +%s%N)

# awk carries the arithmetic because POSIX sh has no floats.
#
# TWO EDITING TRAPS, both measured in #194, both of which report as an awk error
# while being something else:
#
# 1. THE AWK PROGRAM IS A SINGLE-QUOTED SHELL STRING, so it may not contain an
#    APOSTROPHE anywhere — not even inside an awk comment. One in the word
#    "N100's" ended the shell string 40 lines early and mawk reported
#    `line 33: missing } near end of file`, pointing at a comment. If awk claims
#    an unbalanced brace, look for a quote before you look for a brace.
# 2. Target dialect is mawk 1.3.4 (what Debian ships as `awk`). Syntax-check any
#    edit against mawk ON A HOST: BSD awk on the operator Mac accepts programs
#    mawk refuses, so a local check is a different dialect and proves nothing.
# The nanosecond
# stamps (~1.8e18) and the energy counters (<= 2.6e11) both sit inside a double's
# exact-enough range for this: the ulp at 1.8e18 is ~256 ns against a ~1e9 ns
# window, i.e. a relative error of 3e-7.
{
  printf 'T %s %s %s %s\n' "$ta" "$tb" "$tc" "$td"
  printf '%s' "$meta" | sed 's/^/M /'
  printf '%s\n' "$first" | sed 's/^/A /'
  printf '%s\n' "$second" | sed 's/^/B /'
} | awk -v ceiling="$ceiling" '
  $1 == "T" { t0 = ($2 + $3) / 2; t1 = ($4 + $5) / 2; next }
  # Emission order follows discovery order (package-0, core, uncore) rather than
  # awk hash order, so the exec output is stable enough to eyeball in a journal.
  $1 == "M" { n += 1; zone[n] = $2; name[$2] = $3; max[$2] = $4; next }
  # NF == 3 is the read-failure filter: a zone whose energy_uj could not be read
  # printed a path and nothing else, and is dropped rather than zeroed.
  $1 == "A" && NF == 3 { a[$2] = $3; next }
  $1 == "B" && NF == 3 { b[$2] = $3; next }
  END {
    elapsed = (t1 - t0) / 1000000000
    if (elapsed <= 0) {
      print "rapl_power.sh: non-positive elapsed time between sample passes; emitting nothing" > "/dev/stderr"
      exit 1
    }
    for (i = 1; i <= n; i += 1) {
      z = zone[i]
      if (!(z in a) || !(z in b)) {
        print "rapl_power.sh: " z " (" name[z] ") became unreadable during a sample pass; emitting nothing for it" > "/dev/stderr"
        continue
      }
      delta = b[z] - a[z]
      # THE WRAP BRANCH, and it is a GUESS that must be bounded. Both reads come
      # from the same zone, so delta is in [-max, max] and `delta += max` is ALWAYS
      # >= 0 — a second `if (delta < 0)` after it can never fire, which is exactly
      # the unfalsifiable guard CLAUDE.md forbids. It sat here until review, and a
      # counter RESET (energy_uj jumping high->low without reaching max: module
      # reload, kexec, firmware event) was silently "corrected" into a vast number:
      # measured on this host, a 1000000 -> 0 reset on eq12 printed 261339.76 W.
      # That is the same artefact class this script rejects [[inputs.intel_powerstat]]
      # for, so it is bounded by PHYSICS instead: no package here can draw `ceiling`
      # watts (default 1000 W, roughly 40x the N100 ceiling and 12x the Ryzen
      # one), so a corrected value above it is a reset or garbage, not a reading.
      # NOTE: no APOSTROPHES anywhere below -- see the dialect note above.
      if (delta < 0) { delta += max[z] }
      watts = delta / 1000000 / elapsed
      if (watts > ceiling) {
        msg = "rapl_power.sh: " z " (" name[z] ") implausible " watts " W over "
        msg = msg elapsed "s (counter reset, not a wrap?); emitting nothing for it"
        print msg > "/dev/stderr"
        continue
      }
      # b[z] is the RAW second-pass counter, exported so freshness is observable
      # independently of the derived rate (#206 -- see the header). COMMA between
      # the fields: a space would begin the influx TIMESTAMP field instead.
      # %.0f, never %d -- mawk truncates %d at INT_MAX and these counters reach
      # 2.6e11. One printf, so a zone emits both fields or neither.
      printf "rapl,domain=%s power_watts=%.6f,energy_uj=%.0f\n", name[z], watts, b[z]
      emitted += 1
    }
    if (emitted == 0) {
      print "rapl_power.sh: no zone produced a usable sample" > "/dev/stderr"
      exit 1
    }
  }
'

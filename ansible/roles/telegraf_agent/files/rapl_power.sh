#!/bin/sh
# roles/telegraf_agent (#194) — RAPL package power, in WATTS, for both physical
# Proxmox hosts. Emits influx line protocol on stdout:
#
#   rapl,domain=package-0 power_watts=2.671234
#
# -> prometheusremotewrite names it `rapl_power_watts`, tag `domain`; the agent
# stamps `host` (telegraf.conf's `hostname = "<inventory_hostname>"`).
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
# correction is exact rather than a heuristic.
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

# awk carries the arithmetic because POSIX sh has no floats. The nanosecond
# stamps (~1.8e18) and the energy counters (<= 2.6e11) both sit inside a double's
# exact-enough range for this: the ulp at 1.8e18 is ~256 ns against a ~1e9 ns
# window, i.e. a relative error of 3e-7.
{
  printf 'T %s %s %s %s\n' "$ta" "$tb" "$tc" "$td"
  printf '%s' "$meta" | sed 's/^/M /'
  printf '%s\n' "$first" | sed 's/^/A /'
  printf '%s\n' "$second" | sed 's/^/B /'
} | awk '
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
    if (elapsed <= 0) { exit 0 }
    for (i = 1; i <= n; i += 1) {
      z = zone[i]
      if (!(z in a) || !(z in b)) { continue }
      delta = b[z] - a[z]
      # THE WRAP BRANCH. One correction only: a still-negative delta after it
      # means a double wrap or a counter reset, neither of which this can
      # honestly convert to watts, so the zone emits nothing.
      if (delta < 0) { delta += max[z] }
      if (delta < 0) { continue }
      printf "rapl,domain=%s power_watts=%.6f\n", name[z], delta / 1000000 / elapsed
    }
  }
'
exit 0

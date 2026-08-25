#!/bin/sh
# RAPL per-domain package power for the physical Proxmox hosts (#194).
#
# Reads every /sys/class/powercap/intel-rapl:* domain twice, RAPL_SAMPLE_SECONDS
# apart, and emits watts as influx line protocol:
#
#   rapl,domain=package-0 power_watts=2.270
#
# - The domain tag comes from the `name` file, never the sysfs index (the index
#   is not stable across boots).
# - energy_uj WRAPS at max_energy_range_uj (65.5 kJ on n5pro — ~3 h idle, well
#   under an hour loaded). A negative delta inside the sample window is exactly
#   one wrap (a double wrap in 2 s would need >16 kW), corrected by adding the
#   domain's own max_energy_range_uj.
# - This is a 2 s spot sample taken once per collection interval, not a 60 s
#   average — deliberately stateless: a state file would add reboot/stale-state
#   failure classes (boot resets energy_uj, which wrap-correction would turn
#   into a fabricated spike).
# - A domain that cannot be read is SKIPPED (gauges skip on unknown, CLAUDE.md);
#   zero readable domains is a loud rc=1 so telegraf logs the failure.
# - The glob is intel-rapl:* — it deliberately excludes the bare intel-rapl
#   control node and any intel-rapl-mmio:* duplicate of the package domain.
# - RAPL is CPU package power, NOT wall power. Nothing downstream may present it
#   as a machine total.
#
# POWERCAP_ROOT and RAPL_SAMPLE_SECONDS exist so the wrap branch is TESTABLE
# against a fixture — a guard you have not seen fail is not a guard (CLAUDE.md).
set -eu

POWERCAP_ROOT="${POWERCAP_ROOT:-/sys/class/powercap}"
RAPL_SAMPLE_SECONDS="${RAPL_SAMPLE_SECONDS:-2}"

t0=$(date +%s%N)
snap0=""
for z in "$POWERCAP_ROOT"/intel-rapl:*; do
    [ -f "$z/energy_uj" ] || continue
    name=$(cat "$z/name" 2>/dev/null) || continue
    e=$(cat "$z/energy_uj" 2>/dev/null) || continue
    max=$(cat "$z/max_energy_range_uj" 2>/dev/null) || continue
    snap0="${snap0}${z}|${name}|${e}|${max}
"
done

sleep "$RAPL_SAMPLE_SECONDS"

t1=$(date +%s%N)
snap1=""
for z in "$POWERCAP_ROOT"/intel-rapl:*; do
    [ -f "$z/energy_uj" ] || continue
    e=$(cat "$z/energy_uj" 2>/dev/null) || continue
    snap1="${snap1}${z}|${e}
"
done

# dt in the shell: %s%N epochs (~1.8e18) exceed awk's double precision; the
# 64-bit shell subtraction is exact.
dt_ns=$((t1 - t0))

printf '%s' "$snap0" | awk -F'|' -v dt_ns="$dt_ns" -v snap1="$snap1" -v root="$POWERCAP_ROOT" '
BEGIN {
    n = split(snap1, lines, "\n")
    for (i = 1; i <= n; i++) {
        m = split(lines[i], f, "|")
        if (m == 2) e1[f[1]] = f[2]
    }
}
NF == 4 {
    z = $1; name = $2; e0 = $3 + 0; max = $4 + 0
    if (!(z in e1)) next
    d = e1[z] - e0
    if (d < 0) d += max            # exactly one wrap inside the sample window
    gsub(/[ ,=]/, "_", name)       # influx tag-value safety
    printf "rapl,domain=%s power_watts=%.3f\n", name, d / (dt_ns / 1e9) / 1e6
    count++
}
END {
    if (count == 0) {
        print "rapl_power.sh: no readable RAPL domains under " root > "/dev/stderr"
        exit 1
    }
}'

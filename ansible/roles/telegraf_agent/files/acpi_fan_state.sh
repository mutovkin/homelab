#!/bin/sh
# roles/telegraf_agent (#186) — eq12's five ACPI fan objects, as BINARY STATE.
# cur_state is 0/1 on this board (max_state=1, measured 2026-08-24): on/off,
# NOT a tachometer. There is no RPM on either physical host — that negative
# result is recorded in docs/eq12.md and the issue; do not rename this to
# anything speed-shaped.
#
# Gauge semantics per CLAUDE.md: a device that cannot be read emits NOTHING —
# never a fabricated zero. Device indices are not stable across boots; the
# `device` tag is identity-of-the-moment, and the count (5 on eq12) is what the
# deploy asserts.
for d in /sys/class/thermal/cooling_device*; do
  [ -r "$d/type" ] || continue
  [ "$(cat "$d/type")" = "Fan" ] || continue
  cur=$(cat "$d/cur_state" 2>/dev/null) || continue
  case "$cur" in ''|*[!0-9]*) continue ;; esac
  printf 'acpi_fan_state,device=%s state=%si\n' "${d##*/}" "$cur"
done
exit 0

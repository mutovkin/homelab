# telegraf_agent

A **native (systemd) telegraf metrics agent** for the two PHYSICAL Proxmox hosts,
`eq12` and `n5pro`. Before #186 neither machine exported a single non-`vector_*`
series: no CPU, memory, load, uptime, disk or thermal telemetry for the two boxes
every VM and container in the fleet runs on top of. This role collects host
vitals, ZFS kstats, the full measured lm-sensors surface, NVMe SMART, RAPL package
power in watts, and (eq12 only) binary ACPI fan state, and remote-writes them to the
same VictoriaMetrics
the container telegraf on eq12_docker writes to — every sample stamped
`host = {{ inventory_hostname }}`, the fleet-wide convention from #178.

```bash
cd ansible
ansible-playbook playbooks/metrics-agents.yml --check --diff --limit n5pro
ansible-playbook playbooks/metrics-agents.yml --limit n5pro
# or: task deploy:metricsagents:check -- --limit n5pro
```

`eq12_docker` is deliberately NOT in the `telegraf_agents` group. It already runs
telegraf as a container inside `roles/services/observability`, and a second agent
on the same host would double-report it under the same `host` label. That
container's config is a different animal in any case — it carries the TrueNAS
graphite listener, the Grafana `/metrics` scrape and `inputs.docker`, none of
which belong on a hypervisor.

## What it deploys

| Path | Mode | Contents |
| ---- | ---- | -------- |
| `/var/cache/telegraf/telegraf_<ver>-1_amd64.deb` | 0644 | the pinned, checksummed release artefact |
| `/etc/telegraf/telegraf.conf` | 0644 | from `templates/telegraf.conf.j2` |
| `/etc/telegraf/telegraf.env` | **0600 root:root** | the systemd EnvironmentFile (VM credentials) |
| `/etc/systemd/system/telegraf.service.d/10-homelab.conf` | 0644 | the override drop-in |
| `/usr/local/lib/telegraf/acpi_fan_state.sh` | 0755 | eq12 only — the binary fan-state exec input |
| `/usr/local/lib/telegraf/rapl_power.sh` | 0755 | both hosts — the RAPL package-power exec input |

## The shipped unit's traps

The unit inside the 1.39.3 .deb (`usr/lib/telegraf/scripts/telegraf.service`,
read out of the package rather than assumed) is `Type=notify`,
`User=telegraf`, `EnvironmentFile=-/etc/default/telegraf`, `Restart=on-failure`,
with no StartLimit overrides.

`StartLimitIntervalSec=0` removes systemd's default five-starts-in-ten-seconds
rate limit. A collector that retries forever is strictly better than one that
latches `failed` and is never heard from again: liveness here is an absence rule
over the metrics store, not a systemd state nobody polls. `Restart=always` with
`RestartSec=10s` is the other half of that.

`EnvironmentFile=/etc/telegraf/telegraf.env`, with **no `-` prefix**, so a missing
credentials file fails the unit loudly instead of starting an agent that ships
unauthenticated. `/etc/default/telegraf` is a dpkg conffile and is left alone;
EnvironmentFile entries are additive and later entries win.

## Privilege: why not root, why not sudo, why not setuid

**The shipped `User=telegraf` stays.** `[[inputs.smart]]` genuinely needs
elevation — `/dev/nvme*` is `crw------- root root` — but the elevation is an
**ambient capability set**, not an identity change. The drop-in adds
`AmbientCapabilities=CAP_DAC_OVERRIDE CAP_SYS_ADMIN` and `NoNewPrivileges=yes`.

The staircase was measured on **both** hypervisors, 2026-08-25, with
`systemd-run` as an unprivileged user — each rung is a real observation, not a
guess about which capability "should" be enough:

| properties | result |
| ---------- | ------ |
| no capabilities | `smartctl: open /dev/nvme0 failed: Permission denied` |
| `CAP_DAC_OVERRIDE` | device opens, then `NVME_IOCTL_ADMIN_CMD: Permission denied` |
| `CAP_DAC_OVERRIDE CAP_SYS_ADMIN` | full `smartctl -H` / `-x` succeeds |
| same + `NoNewPrivileges=yes` | **still succeeds** |

That last row is the point. **Why not setuid:** CLAUDE.md's ping lesson is that
`no_new_privs` strips setuid and file capabilities at `execve`, which silently
cost the container telegraf eight ping metrics while it stayed healthy. Ambient
capabilities are neither setuid nor fscaps — they are inherited across `execve`
into the child (`smartctl`) regardless of `no_new_privs`. So this is the clean
answer to that lesson rather than a carve-out from it, and the deploy proves it
the way the lesson demands: by asserting the smart input's OWN output from
`telegraf --test`, run under exactly these unit properties.

**Why not sudo:** `sudo` is not installed on either hypervisor (measured
2026-08-24). Installing it on a Proxmox host to grant a metrics agent one binary
is a larger posture change than two scoped capabilities.

**Why not root:** because it is not necessary. Only the NVMe admin ioctl needs
anything at all — `sensors`, the ZFS kstats under `/proc/spl/kstat/zfs`, and
eq12's fan `cur_state` (0644) need **no** capability, and the root-only RAPL
`energy_uj` is covered by `CAP_DAC_OVERRIDE` alone — measured before #194 relied
on it.

Because the unit does hold two strong capabilities, they are paid for with
confinement everywhere else: `ProtectSystem=strict` with
`ReadWritePaths=/var/lib/telegraf` (the postinst-created statefile dir,
`root:telegraf` 770 — the unit's only writable path), `ProtectHome=yes`,
`PrivateTmp=yes`, and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
AF_NETLINK` (AF_UNIX for `sd_notify`, INET/INET6 for the HTTP output, NETLINK for
gopsutil's interface enumeration). Nothing in this role writes to sysfs — it is
read-only collection, no fan or thermal *control*, no powercap writes.

**If one of those hardening directives ever breaks an input, relax THAT
directive and record the measured failure beside it. Do not jump to
`User=root`.**

## Two-leg download, and the update path

The .deb is fetched by the **controller** first (`run_once`, into
`~/.cache/homelab/telegraf_agent`) and shipped to each host; a direct download on
the target is the fallback used only when the controller could not get it. Both
hypervisors currently reach `dl.influxdata.com` (measured 2026-08-24) — the
two-leg shape is kept anyway because n5pro's flat refusal of github.com proved
upstream policy here can be selective, and the shape costs nothing while making
the role work on a restricted or air-gapped host. Neither leg has a stat-exists
gate: `get_url` hashes whatever is on disk, skips on a match and re-downloads on a
mismatch, so a corrupt cache is repaired rather than trusted forever (CLAUDE.md).

Both legs are `ignore_errors: true` **and registered**, never `failed_when: false`
— the latter *assigns* `failed=False`, which made leg 1b run on a failed fetch and
left the fallback leg unreachable dead code until #186's review round measured it.
The loud failure belongs to the arrival assert, which reads the real `.failed`
values to name the leg that broke; a failed leg shows as `failed(ignored)` in the
recap.

**A native package on a Proxmox host has no watchtower and no `pull: always`, so
the update path has to be named or there is not one.** It is:

1. Edit `telegraf_agent_version` **and** `telegraf_agent_deb_sha256` in
   `defaults/main.yml` — together, in one commit. Verify the sha256 against the
   checksum table in that release's own GitHub release notes, not against the
   file you just downloaded.
2. Re-run `task deploy:metricsagents`.
3. The .deb's postinst `try-restart`s the running unit onto the config this role
   has *already* templated (config is written BEFORE the package, deliberately),
   and the role's arrival asserts then prove the bumped agent still delivers.

No mutable tag exists anywhere in this path. That is a real difference from the
eq12_docker container telegraf, which floats on `telegraf:latest` and moved to
1.39.3 under #187's apply on 2026-08-25. The two WILL drift, and the same
follow-up that owns vector's identical container-vs-native drift note owns this
one.

## RAPL package power (#194)

Both hosts export `rapl_power_watts` — and, since #206, `rapl_energy_uj` beside
it on the same line — tagged `domain`, every 60 s: `package-0` + `core` +
`uncore` on eq12, `package-0` alone on n5pro. Both come from
`files/rapl_power.sh`, an exec input, and every choice below is a measurement
rather than a preference.

**It is `[[inputs.exec]]`, not `[[inputs.intel_powerstat]]`, and both halves of
that were measured 2026-08-25 with `telegraf --test` under the real unit user and
capabilities.** On n5pro the plugin will not even initialise — *"failed to
initialize metric fetcher interface: host processor is not supported"* — so it
cannot serve the AMD box at all. On eq12 it does run, and its **first** gather
emits `current_power_consumption_watts=826963607.79`: an absurd first-sample
artefact, because there is no prior read to delta against — i.e. a garbage spike
at every agent restart even where the plugin works. Running the plugin on eq12
and a script on n5pro would then export two metric names for one signal — the
`coretemp`/`k10temp` divergence trap one layer up. One script on both hosts
instead. **Do not "simplify" this back to the plugin.**

**Watts are computed at the source because these counters wrap fast.**
`max_energy_range_uj` is 262143328850 uJ on eq12 but only 65532610987 uJ
(65.5 kJ) on n5pro — a rollover roughly every 3 h at idle and closer to 20 min
under load. A `rate()`/`increase()` over the raw counter emits a large negative
spike at every wrap. The script takes two `energy_uj` reads ~1 s apart, adds that
zone's own `max_energy_range_uj` to a negative delta, and divides by the
**actual** elapsed time measured between the two read passes rather than an
assumed 1.0 s. Over a ~1 s window a *double* wrap is physically impossible (it
would need >65 kW on n5pro), so a single correction is the only one that can be
needed.

**But the correction is a GUESS about what a negative delta meant, and it is
bounded by physics.** A counter RESET (`energy_uj` dropping high->low without
reaching max: module reload, kexec, a firmware event) has the same sign as a
wrap. This paragraph used to claim that "a delta still negative after the
correction is a counter reset, and that zone emits nothing" — that was false, and
the guard backing it was unreachable: both reads come from the same zone, so
`delta` is in `[-max, max]` and `delta += max` can never leave it negative. A
reset was therefore silently converted into a vast number — measured on eq12, a
`1000000 -> 0` reset printed **261339.76 W**, the same artefact class this script
rejects `[[inputs.intel_powerstat]]` for. Corrected values above
`RAPL_MAX_PLAUSIBLE_WATTS` (default 1000, ~40x the N100's ceiling and ~12x the
Ryzen's) are now discarded with a message on stderr.

The wrap branch **was exercised**, manually during #194, against a stub tree via
the `RAPL_SYSFS_ROOT` override — the only reason that override exists. No harness
is committed, so treat this as a recipe to re-run rather than a test that runs
itself, and do re-run it after any edit to the awk stage: it is how both the
unreachable guard and the reset path above were found.

**Each sample is a ~1 s instantaneous snapshot taken once per 60 s interval, not
a 60 s average.** Say so on any panel that could be read as energy. Widening the
agent interval does not make the snapshot more representative — it only makes the
snapshots rarer, and a wide interval is what lets a wrap land in a sampling gap.
Keep it at 60 s.

**The domain label comes from the sibling `name` file, never the sysfs node
index** (`intel-rapl:0` -> `package-0`, `intel-rapl:0:0` -> `core`). Indices are
not stable across boots or hardware.

**Domain coverage is a property of the BOARD, and the only test that establishes
it is LOAD PLACEMENT.** The two hosts give opposite answers for the same sysfs
path and the same domain name:

| placement | eq12 package / core | n5pro package / core |
| --------- | ------------------- | -------------------- |
| idle | 1.70 / 1.62 W | 5.93 / 0.06 W |
| busy-loop on CPU0 | 11.47 / 11.39 W | 21.96 / 15.83 W |
| busy-loop on a non-CPU0 core | 12.13 / 12.05 W | 11.21 / 0.16 W |
| busy-loop on three non-CPU0 cores | 19.55 / 19.46 W | 33.93 / 0.12 W |
| idle again | 1.86 / 1.78 W | 6.32 / 0.09 W |

(measured 2026-08-25; eq12 loaded CPU3 and CPU1+2+3, n5pro CPU20 and CPU2+14+20)

eq12's `core` tracks package to within ~0.08 W at every placement — a genuine
all-core aggregate, exported. n5pro's stays flat at ~0.12 W while package climbs
28 W under a three-core load with no CPU0 in it — it is **one physical core of
twelve (24 logical CPUs)**, and is dropped at the source via
`telegraf_agent_rapl_exclude_domains: [core]` and asserted absent from
`telegraf --test`'s own output. Its `uncore` does not exist at all; eq12's does
and is the iGPU, flat rather than absent at idle.

**Why placement and nothing weaker.** A busy-loop pinned to CPU0 raises `core`
under BOTH hypotheses — real aggregate and CPU0-only — so it cannot separate
them. Neither can an idle-time comparison against the per-core MSRs, because at
idle an aggregate approximates its busiest core. During #194 both of those tests
were run, by different people, and produced confident and *opposite* wrong
conclusions. The generalisable rule, which is the most valuable thing this
collector taught us:

> An experiment that cannot distinguish between the hypotheses is not evidence,
> however careful it looks. Ask what result would falsify the alternative, then
> design the measurement for that.

n5pro's `core` is dropped rather than relabelled `domain="cpu0"`: one physical
core of twelve answers no question anyone asks, and a per-core series under the same
metric name as `package-0` invites a sum that is always wrong. This is not a
fabricated zero — it is a real reading of the wrong thing, which is worse,
because it looks measured.

**A FROZEN counter reads as a confident `0.000000 W`, and two things now catch
it (#206).** The script's contract is that a zone it cannot read completely emits
nothing. That covers *unreadable*; it does not cover *readable but never
advancing* — a domain the driver still exposes while the firmware has stopped
updating it. Measured: with `energy_uj` static across both passes the script
emits `power_watts=0.000000`, which is a faithful reading of the counter (energy
consumed in the window really was below 1 uJ) and is therefore NOT suppressed at
the source — eq12's `uncore` legitimately reads exactly that at idle. Both
absence rules count DOMAINS, and a frozen domain is still present, so neither
fires and the dashboards render a legitimate-looking zero.

*The detector:* `obs-rapl-power-frozen-eq12` / `-n5pro` in
`roles/services/observability/files/data/grafana/provisioning/alerting/telegraf-health.yaml`,
`max_over_time(rapl_power_watts{host="…",domain="package-0"}[6h])` under a
threshold of 1 mW, `noDataState: OK` (absence is the sibling rule's job — #151),
`severity: warning`. Detection latency is ~6 h 10 m worst case: the window has to
age out the last live sample, then the group's 5 m interval ticks, then `for: 5m`
holds. A frozen counter is a forever-shaped fault, so that is a deliberate trade
against ever false-firing.

*The observable:* the script now emits the raw second-pass counter as a second
field on the same line — `rapl,domain=package-0 power_watts=…,energy_uj=…` →
`rapl_energy_uj` — because the derived wattage DESTROYS the freshness
information. `changes(rapl_energy_uj{host="eq12",domain="uncore"}[30m])` asks
"is this domain still advancing?" of ANY domain, without an ssh session. It
wraps at `max_energy_range_uj` (sawtooth): it is a freshness observable, never a
rate source. Two implementation notes that are one character wide each — the
fields are separated by a COMMA (a space begins the influx TIMESTAMP field, and
`energy_uj=81017680655` then parses cleanly as a 1970 timestamp on a
single-field point), and the format is `%.0f`, never `%d`, because mawk 1.3.4
truncates `%d` at INT_MAX while these counters reach 2.6e11. Both fields ride
ONE printf, so a zone emits both or neither and the domain sets cannot diverge;
the deploy asserts that from VictoriaMetrics anyway, because the parse and the
output path are not covered by that coupling.

*Why the alert is scoped to `package-0` and nothing else.* "Near-zero watts"
cannot discriminate frozen from idle for a domain that legitimately idles at
zero, and that is measured rather than assumed (2026-08-29, eq12 `uncore`): 268
of 1440 samples in 24 h were exactly `0.000000`, and 2 of 193 sampled one-hour
windows were entirely zero. Pointed at `uncore` this rule would not merely risk a
false alarm — it would fire **permanently**: that domain's 6 h maxima measured
**0.000121–0.000305 W** over those four days, entirely below the 1 mW threshold.
Measured side by side at 22:44Z with the rules deployed and `inactive`: `uncore`
6 h max `0.000304` (would alert), eq12 `package-0` `13.499178`, n5pro
`package-0` `9.314859`. `package-0` is different in kind: a running machine
always draws package power. Over the family's full retained history (2026-08-25
22:00Z → 2026-08-29 22:00Z, 193 six-hour windows per host) the lowest single
sample was **1.492526 W on eq12** and **5.382494 W on n5pro**, and zero windows
on either host contained a zero at all. The 1 mW threshold sits ~1490x below the
weaker machine's floor, and the domain pin is enforced by the static reconciler
in `roles/services/observability/tasks/main.yml` — widening the selector by one
label is a one-word edit with a permanent alarm on the other side of it.

*Two known residuals, stated rather than implied.* (1) eq12's `core` idles at
1.62–1.78 W, so its zero is as physically impossible as `package-0`'s — and a
frozen `core` alone keeps the domain count at 3 and pages nobody, the same shape
this rule exists for. It is uncovered deliberately: every freeze mechanism we
know of (powercap driver fault, firmware event, kexec) acts on the MSR interface
as a whole and would take `package-0` with it, which the rule does catch. The
generalisation — a rule per domain whose idle floor is provably non-zero — belongs
with #228, thresholded from history rather than from that argument. (2) The
absence rules count DOMAINS, not names, so a `package-0` that vanished and was
replaced in the same window (`psys` appearing across a BIOS update) keeps the
count green while the frozen rule sits at NoData → OK. The check that catches a
substitution is the deploy-time domain-set EQUALITY assert, which only runs on a
deploy.

*The honest limit.* For a domain that idles at zero, only ADVANCEMENT is
provable — a domain whose firmware has genuinely gated it to zero draw is
indistinguishable from a frozen one at ANY window in the value domain, which is
why the answer for those domains is the raw counter and not a threshold. An
all-domain freshness rule (`changes(rapl_energy_uj[…]) == 0`) is deliberately
NOT added here: nothing yet bounds how long a healthy idle counter may
legitimately sit static, and choosing that window from today's four days of
history would be the confounded guess #194 exists to prevent. Threshold it from
accumulated `rapl_energy_uj` history — that is **#228**, which carries the
acceptance bar and every number below. Measured input for whoever does: eq12 `uncore` advanced 488 uJ across 60 s
at idle (8 counter quanta of 61.035 uJ), with `package-0` advancing 355 753 179
uJ over the same 60 s as the positive control — so at the 60 s collection
interval consecutive samples of a live-but-idle counter do differ, and
`changes()` over minutes is a real discriminator rather than a hopeful one.

**The delivered domain set is asserted for EQUALITY, not presence.** The script
emits nothing for a zone it cannot read completely (gauges skip on unknown), so
losing one domain thins the series silently while a presence check stays green —
and equality also fails if an *excluded* domain reappears. The set lives in
`telegraf_agent_rapl_expected_domains` per host, and the role refuses to run with
an empty one: that would make the equality trivially true against an empty
result.

**Liveness is a SECOND pair of rules, deliberately.**
`obs-rapl-power-absent-eq12` and `-n5pro`, in
`roles/services/observability/files/data/grafana/provisioning/alerting/telegraf-health.yaml`,
watch `count by (host) (last_over_time(rapl_power_watts{host="..."}[10m]))`
against that host's domain count. The agent-liveness rules above them watch the
`[[inputs.system]]` stream, and an exec input can die on its own while that
stream keeps flowing perfectly — #174's "two delivery paths need two liveness
rules", one family over. These two are `severity: warning`, which routes
email-only under the root policy: losing power visibility is not a page, and the
agent-dead case that IS one is already owned at `critical`.

**Read-only collection.** Nothing here writes to `powercap` — no power capping,
no `constraint_*` file, no policy change. And RAPL is **package** power, never
wall power: it excludes NVMe, fans, PSU conversion loss, and on n5pro the drive
bays and the whole NAS side of the box. Neither machine has a wall-power meter,
and adding one is a hardware purchase (explicitly out of scope in #194).

## Fans: what is collected, and what is only reachable

**No fan tachometer is collected by this role**, and under the DEFAULT driver set
neither host exposes one. Measured 2026-08-24: no `fan*_input` and no `pwm*` under
`/sys/class/hwmon/hwmon*/` on either machine, and no Super-I/O-class fan driver
loaded (no `nct6775`, `it87`, `w836*`, `f718*`) — `coretemp` is the only CPU-temp
module on eq12, `k10temp` the only one on n5pro.

Past that default reading the two hosts stop being the same story, and the
difference is worth stating precisely:

- **eq12's tachometers are REACHABLE.** A forced binding of the *stock*, in-kernel
  `it87` module reads two real fans — measured 2026-08-25. It is **not persisted,
  not exported, and pending an operator decision**; the command, the corroboration
  and the reboot-to-default check are in [docs/eq12.md](../../../docs/eq12.md).
- **n5pro's negative is definitive.** Its Super-I/O chip ID reads `0x5571`, which
  no kernel driver claims; the chip is **community-identified** as an ITE IT5571,
  and **community EC dumps** of this machine read zeros in the fan registers, so
  even a purpose-written driver would find nothing there. Our own measurements are
  the raw ID and `sensors-detect`'s empty scan — the identification and the dumps
  are attributed, not ours. See [docs/n5pro.md](../../../docs/n5pro.md).

What this role does collect:

- **eq12** — five ACPI fan objects (`PNP0C0B:00`–`04`), surfaced as thermal
  cooling devices reading `Fan cur=0 max=1`. That is **binary on/off state**.
  `files/acpi_fan_state.sh` exports exactly that, as `acpi_fan_state` with a
  `state` field of 0 or 1. It is a real signal and worth having; it is **not** a
  speed. Never label, graph, alert on, or rename it as RPM.
- **n5pro** — no ACPI fan objects at all, and no fan-capable hwmon chip. There is
  no fan signal of any kind on this board, so
  `telegraf_agent_acpi_fan_state` is false for it and nothing pretends otherwise.

`sensors-detect` probes ISA/Super-I/O address space and writes module config, so
it is a deliberate, separately-scoped change — this role must never run it.

## Fabricated zeros

`amdgpu` on n5pro reports `vddgfx` and `vddnb` as **0.000 V** — unsupported
readings, not measurements. They are dropped at the source by the
`[inputs.sensors.tagdrop]` block, and the deploy asserts against
`telegraf --test`'s own output that they never appear. This repo has been bitten
by exactly this twice (`_devicename_sda` reporting 0 °C on TrueNAS, and telegraf's
silently-lost ping metrics), which is why the exclusion has a guard rather than
just a comment. If that assert ever fires, fix the filter — do not delete the
assert.

## The per-host proof lists

`telegraf_agent_expected_test_output` / `telegraf_agent_forbidden_test_output` in
each host's host_vars are LITERAL SUBSTRINGS (not regexes) the role checks against
`telegraf --test`'s output before it will finish. `telegraf_agent_forbidden_test_output`
is the PROOF list only; what the sensors input actually drops at the source is the
separate `telegraf_agent_sensors_tagdrop_features` — one shapes the config, the
other verifies the result, and they are kept in sync per host deliberately. They are the input's OWN
produce, not "the process started" — CLAUDE.md's ping lesson, where eight metrics
went to NO DATA while the container stayed healthy. The expected list must be
non-empty for every host in the group, and the role asserts that too: an empty
list would silently turn the whole proof into `assert: true`.

The two boards genuinely differ, which is why these live in host_vars rather than
in a shared default. eq12 has `coretemp` with Package + Core 0..3;
n5pro's `k10temp` gives **one** `Tctl` value and no per-core temps at all (and
`Tctl` on AMD can carry an offset, so nothing should put a threshold on it
without a second source), plus two `spd5118` DDR5 DIMM sensors and an `amdgpu`
iGPU block that is the only host-side view of what CT 201's VAAPI workloads cost.
Any dashboard written against eq12's five coretemp readings shows empty panels for
n5pro unless it is written for both shapes.


### The per-host COUNTS (#202)

Two more per-host facts sit beside the substring lists, and they are counts
rather than substrings because the failure they exist for is a family *thinning*
rather than vanishing:

| variable | eq12 | n5pro | what a low value means |
| -------- | ---- | ----- | ---------------------- |
| `telegraf_agent_expected_sensor_series` | 7 | 9 | chips went unreadable — an hwmon module renamed or stopped loading |
| `telegraf_agent_acpi_fan_expected_count` | 5 | *(n/a)* | cooling devices went unreadable; `files/acpi_fan_state.sh` emits nothing for one it cannot read rather than a fabricated zero |

Both default to `0`, and `0` is the "nobody declared this" value, not a plausible
count: the arrival proofs compare for **equality**, so a declared 0 would be
satisfied by an empty result — green precisely when the family had stopped. The
role asserts both are non-zero before it will run.

The fan count used to be the literal `'5'` inside the role's assert, with *the
same 5* independently written into the `obs-fan-state-absent-eq12` alert rule —
two copies of one board fact with nothing reconciling them, which is the
#188/#194 matched-pair defect. Both now read this variable, and a static guard in
`roles/services/observability` fails the deploy if the rule's threshold drifts
from it.

**The deploy assert compares EQUALITY while the alert rule compares `lt`, and the
asymmetry is deliberate.** A sensor set can grow with no change to this repo: a
7-day windowed count on eq12 (2026-08-25) returned the seven steady series at
1050 samples each *plus* `it8622-isa-0a30` `temp1`/`temp2` at **one sample each**
— the one-off forced `it87` binding recorded in `docs/eq12.md`. An instant
`count()` hides that completely. Paging an operator for loading a module would be
wrong, so the alert is one-sided; but a deploy is exactly the moment to notice
that the declared model no longer matches the machine, so the deploy is strict.

### The SMART proof anchors on `health_ok`, and the regex it replaced could not fail

`smart_device_*` is the only family needing elevated privilege, and it fails in a
shape that defeats the obvious check. Measured on eq12 2026-08-25 by running
`telegraf --test` under `setpriv --reuid=65534` — the real failure mode, the unit
losing its ambient capabilities so `smartctl` opens the device and the NVMe admin
ioctl is denied:

```
> smart_device,device=nvme0 exit_status=2i
```

**One field of fourteen survives**, and the `model` and `serial_no` tags go with
the rest. The arrival proof used to query `{__name__=~"smart_device_.+"}` and
require a non-empty result — and `smart_device_exit_status` matches that regex.
So the proof would have **passed on the exact capability regression its own
`fail_msg` names as the first thing to check**. It now anchors on
`smart_device_health_ok`, a field that only exists after a successful read.

The two neighbouring failures are not silent, measured in the same run, and are
deliberately not this proof's job:

| case | result |
| ---- | ------ |
| `smartctl` runs but fails at `--scan` (`path_smartctl = /bin/false`) | plugin emits **nothing** — an ordinary absence |
| `smartctl` missing (`path_smartctl = /nonexistent`) | telegraf **refuses to start**: `could not initialize input inputs.smart` — so `system_uptime` stops and the liveness rule pages |
| unprivileged (`setpriv --reuid=65534`) | **`exit_status=2i` only** — the silent one, and what this anchor exists for |

Known limit, stated rather than left implicit: the proof is `> 0` devices, not a
pinned count, because both hosts have exactly one SMART device and there is no
host_vars declaration of disks to compare against. With a *second* device
installed, losing one of the two would not fail it. The fix at that point is a
`telegraf_agent_smart_expected_devices` declaration plus the reconciler the
sensors and fan counts already have — added in the same change that installs the
disk, not retrofitted afterwards.

### Freshness: every family, not just `system_uptime` (#207)

Every family proof is issued shortly after a restart, and `flush_interval` is
10s — so the process the run **replaced** has a final batch at most ~90s old,
sitting comfortably inside every one of those windows. A regression that killed a
family's production while leaving the agent healthy would be satisfied by samples
a program that no longer exists had written.

The role's original freshness gate covers `system_uptime` only, and its comment
used to conclude that the family asserts inherited that proof because "every
family rides the same batched output". That is true of **delivery** and false of
**production**: an `[[inputs.exec]]` family is produced by a separate program,
and `telegraf --test` rehearses it *outside* the unit — no `ProtectSystem=strict`,
no `PrivateTmp`, no ambient capabilities. A confinement regression is exactly the
class that survives the rehearsal and dies under the unit, which is why an
arrival proof runs after the restart at all.

So every family now carries its own anchor, in one loop rather than four copies:

```
min(timestamp(<BARE selector>))  >  telegraf_agent_restart_epoch
```

- **the selector must be BARE.** Measured in #194: wrapping it in
  `last_over_time` makes VictoriaMetrics evaluate a subquery on a **5-minute
  grid** and return the grid point, not the sample time — up to 5 minutes stale,
  failing on a perfectly healthy restart.
- **`min()`, not `max()`.** `max()` passes as soon as one series in the family has
  turned over, which for a 9-series family is a proof about one ninth of it.

**Count and freshness are complementary; neither is redundant.** The count asserts
are the only thing that can see a series stale enough to have dropped out of
VictoriaMetrics' 5m instant lookback entirely — a series that is not returned
cannot drag down a `min()`. Freshness is the only thing that can see residual
samples, which count perfectly. Deleting either leaves a real gap.

A single loop introduces a failure mode four hand-written pairs did not have: a
conditionally-built list that comes out short reports `ok` for every family it
does not contain. So the list is checked against an **independently computed**
arithmetic count (re-deriving the list would be circular) and every selector is
checked against this host's own name. That guard runs unconditionally, including
under `--check`, so a broken list is caught on a converged host rather than only
on the run that happens to restart telegraf.

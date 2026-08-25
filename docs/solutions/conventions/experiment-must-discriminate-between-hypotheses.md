---
title: "An experiment that cannot distinguish the hypotheses is not evidence, however careful it looks"
date: 2026-08-25
category: conventions
module: ansible
problem_type: convention
component: observability
severity: high
applies_when:
  - "deciding whether a sensor/domain/counter is real, unsupported, or measuring something other than its name"
  - "a reading is near zero and you are about to call it a fabricated zero"
  - "a load/stress test is used to prove a metric responds"
  - "cross-checking one interface (sysfs) against another (MSR) at idle"
  - "two people reached opposite conclusions from measurements that both looked careful"
  - "a metric's name implies a scope you have not verified on this specific hardware"
related_components:
  - telegraf_agent
  - services/observability
  - rapl
  - powercap
tags:
  - verification
  - measurement
  - experiment-design
  - falsifiability
  - fabricated-zero
  - rapl
  - telegraf
  - review-loop
---

# An experiment that cannot distinguish the hypotheses is not evidence, however careful it looks

## Context

Issue #194 added RAPL package-power collection to the two physical Proxmox hosts. One
sub-question — *is n5pro's `intel-rapl:0:0` "core" domain real?* — was answered
**confidently and wrongly three times**, by three different actors, from three
measurements that were each careful, each correct as far as they went, and each
**incapable of distinguishing the two hypotheses on the table**.

The hypotheses:

- **H1** — `core` is a genuine all-core aggregate, reading ~0 W at idle because Zen 5
  power-gates idle cores.
- **H2** — `core` is one core's counter (CPU0), mislabelled as a domain aggregate.

Both predict a near-zero idle reading. Both predict a large rise when CPU0 is busy. Any
experiment that only produces those two observations selects neither.

## What was actually run, and why each attempt failed

| attempt | evidence | why it could not discriminate |
| ------- | -------- | ----------------------------- |
| issue text | `core` reads 0.04 W beside a 6 W package — "the signature of an unsupported reading" | magnitude alone is not a mechanism; H1 predicts exactly this at idle |
| branch A | at idle, powercap `core` matches CPU0's `MSR_AMD_CORE_ENERGY_STATUS` to 0.06% | **at idle an aggregate approximates its busiest core** — the match is what H1 predicts too |
| coordinator | a single-core busy-loop moved `core` 0.04 W -> 11.46 W, so the domain "is REAL" | the load ran on **CPU0**, which is inside the domain under H2 as well — both hypotheses predict the rise |

Branch A concluded "exclude it". The coordinator concluded "export it" and wrote into the
worker's brief that the question was *"SETTLED by measurement"*. The two conclusions were
opposite; the underlying evidence was equally uninformative in both cases.

## The test that does discriminate

Ask what result would **falsify the alternative**, then build for that. Under H2, a load
on a core the domain does *not* cover leaves it flat while package rises. Under H1 it
rises. So: **place the load off CPU0.**

n5pro (Ryzen AI 9 HX 370, 24 logical CPUs), 6 s windows, two independent runs
(run 2 in parentheses):

| placement | package | core |
| --------- | ------- | ---- |
| idle | 5.93 W (5.83) | 0.06 W (0.05) |
| busy-loop on CPU0 | 21.96 W (22.84) | 15.83 W (16.59) |
| busy-loop on CPU20 | 11.21 W (11.88) | 0.16 W (0.13) |
| busy-loop on CPU2+14+20 | 33.93 W (33.78) | 0.12 W (0.24) |
| idle again | 6.32 W (6.16) | 0.09 W (0.11) |

`core` stays flat at ~0.12 W while package climbs **28 W**. H1 is dead. `intel-rapl:0:0`
on this board covers CPU0 alone.

The same test on eq12 (Intel N100) is the **positive control**, and it is why the test is
trustworthy rather than merely convenient — it can come back the other way, and does:

| placement | package | core |
| --------- | ------- | ---- |
| idle | 1.70 W | 1.62 W |
| busy-loop on CPU0 | 11.47 W | 11.39 W |
| busy-loop on CPU3 | 12.13 W | 12.05 W |
| busy-loop on CPU1+2+3 | 19.55 W | 19.46 W |
| idle again | 1.86 W | 1.78 W |

Same sysfs path, same domain name, opposite answer. **Domain coverage is a property of the
board, not of the name.**

## The convention

1. **Before running a measurement, write down what the competing hypotheses predict.** If
   they predict the same observation, the measurement is not worth running — design a
   different one. This is cheap and it is the whole lesson.
2. **A near-zero reading is not proof of an unsupported sensor, and a reading that moves is
   not proof of a correct one.** Both need a discriminating test.
3. **Always run the positive control** on hardware where you expect the opposite answer. A
   test that cannot come back negative is the same failure one level up — see
   [a verification is only evidence if its instrument can tell the fixed state from the
   broken one](verification-instrument-must-distinguish-fixed-from-broken.md).
4. **Record the confound, not just the conclusion**, in the place the next person will
   edit. Both host_vars files, the role README and `docs/{eq12,n5pro}.md` carry the tables
   above precisely because the issue text argued the opposite and would otherwise be
   "fixed" back.
5. **"Measured" in a brief is not a warrant.** Ask *what placement / what window / what
   would have falsified it*. Three actors here inherited a confident wrong prior because
   the word "measured" ended the conversation.

## Consequence in this repo

- `telegraf_agent_rapl_expected_domains` is a per-host **exact set**, asserted for equality
  against VictoriaMetrics: eq12 `[package-0, core, uncore]`, n5pro `[package-0]`.
- n5pro's `core` is **dropped, not relabelled** `domain="cpu0"`. One core of twelve answers
  no question anyone asks, and a per-core series sharing a metric name with `package-0`
  invites a sum that is always wrong. It is not a fabricated zero — it is a real reading of
  the wrong thing, which is worse, because it looks measured.

## The count, stated plainly: five times, on one question, by everyone who touched it

This is the most important part of this document, and it is not a confession — it is the
evidence for why the rule has to be procedural.

**Five instances of the same error, all on the single question "is n5pro's `core` domain
real?", by four agents and the operator:**

1. **The issue text** — "0.04 W beside a 6 W package has the exact signature of an
   unsupported reading". Magnitude is not a mechanism; a power-gated aggregate reads this.
2. **Branch A** — an idle-time match between powercap `core` and CPU0's energy MSR, to
   0.06%. True, and uninformative: at idle an aggregate approximates its busiest core.
3. **Branch B** — exported the domain on the strength of the above being wrong, without
   testing it either.
4. **The operator** — a single-core load moved `core` 0.04 → 11.46 W, concluded "SETTLED by
   measurement", and wrote that into the implementing agent's brief. The load ran on CPU0,
   which the domain covers under *both* hypotheses.
5. **The implementing agent — inside this very change, in the act of writing this
   document** — asserted that eq12's `uncore` "is flat at idle because there is nothing
   drawing, not because it is unsupported", with no discriminating test. Both hypotheses
   predict flat-at-zero. Caught in review; the claim is now limited to what was measured
   (the counter advances, so it is live) with load-tracking marked explicitly untested.

And a sixth, in the sibling family: the same agent deleted an unreachable `if (delta < 0)`
guard for being unfalsifiable, then **two commits later reintroduced the identical shape**
— an unreachable `''` arm in a `case` whose input `${VAR:-default}` can never be empty.

**None of this was carelessness.** Every one of those measurements was carefully taken and
correctly reported. That is the whole point: **a confounded test feels exactly like
evidence from the inside.** There is no amount of care that fixes it, and "be more
rigorous" is not a control — everyone involved here was already being rigorous.

The control is procedural, and it is one sentence:

> Before believing a measurement, state what result would have **falsified the
> alternative**, and check that your test could actually have produced that result.

If your experiment could not have come out the other way, it is not evidence, no matter how
clean the number is. Write the falsifier down *before* the measurement, because afterwards
the number will look like it settles the question.

## A postscript, because it happened again inside this very issue

While verifying the collector, the dead-zone test used `chmod 000` on a stub file **while
running as root** — and root bypasses permission checks. The zone stayed readable, both
stub zones showed 0 W from a static fixture, and the harness reported "FABRICATED a value
for a dead zone". The bug was in the test, not the collector. Re-run with the file actually
`rm`ed, the collector emitted nothing for the dead zone and a correct value for the
survivor.

The instrument must create the condition it claims to test. Checking that is the same
question as above, asked of your own test rig.

Review then found two more instances of the identical error in the same change, which is
why this section exists rather than a footnote:

**The unreachable wrap guard.** The collector corrected a wrapped counter with
`if (delta < 0) { delta += max }` and then claimed safety with a second
`if (delta < 0) { continue }`. Both reads come from the same zone, so `delta` is in
`[-max, max]` and the corrected value is never negative: the second guard **cannot fire**.
Three separate comments asserted the behaviour it was supposed to provide. A counter
*reset* (energy dropping high→low without reaching max) has the same sign as a wrap and
was silently converted — measured on eq12, a `1000000 → 0` reset emitted
**261339.76 W** from a 6 W part. The fix is a bound the hypothesis can actually fail
against: a corrected value above `RAPL_MAX_PLAUSIBLE_WATTS` is discarded and logged. The
repo rule "a guard you have not seen fail is not a guard" applies to **awk and shell**,
not only to Ansible's `failed_when`.

**A syntax check in the wrong dialect proves nothing.** The fix above was validated with
the operator Mac's BSD awk, which accepted it. The hosts run **mawk 1.3.4**, which did not
— and the deploy failed. Worse, the *reason* was not awk at all: the awk program is a
single-quoted shell string, and an apostrophe inside an awk **comment** (the word
`N100's`) closed that string forty lines early. mawk reported
`line 33: missing } near end of file`, pointing at a comment. So: validate embedded
interpreters against the interpreter **on the host**, and when awk reports an unbalanced
brace, look for a quote before you look for a brace.

**A bound that fails OPEN when its own input is unvalidated.** The plausibility ceiling
added above was passed to awk as `-v ceiling="$RAPL_MAX_PLAUSIBLE_WATTS"`. awk takes a
non-numeric `-v` value as a **string**, so `watts > ceiling` silently became a string
comparison (`"2.32" > "abc"` is false) and every implausible reading passed. The guard
stopped guarding without any sign. A guard's own inputs are part of the guard: validate
them, and prefer failing closed. (Measured: `RAPL_MAX_PLAUSIBLE_WATTS=abc` → all domains
emitted, nothing rejected.)

**A pinned dependency's deprecation warnings are the bump-blocker list.** The same change
shipped `commands = ["/path/script arg"]` to telegraf, which had deprecated that form in
1.39.0 and removes it in **1.45.0** — and said so in the hosts' own journal on every single
start, for the whole life of the deployment. Nobody had read it, because everything worked.
This repo pins `telegraf_agent_version` and bumps deliberately, which is exactly the setup
where a deprecation is a scheduled outage rather than a warning. Read the journal of the
version you pinned, not just the release notes of the version you are moving to. A related
trap in the same finding: the deprecation was of the *value form*, not of embedded
whitespace, so a single-token command warned identically — do not assume the simple case is
exempt from a deprecation you have only seen on the complex one.

The thing that caught both was the role's existing pre-flight assert — it runs
`telegraf --test` and requires each declared family in the **output**, so a collector that
parses on the author's laptop and dies on the host fails the deploy instead of quietly
shipping an agent that is green and missing one family.

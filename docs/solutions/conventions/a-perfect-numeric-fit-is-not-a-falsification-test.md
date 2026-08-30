---
title: "A hypothesis that fits the numbers is not thereby tested — and the better the fit, the less anyone looks"
date: 2026-08-30
category: conventions
module: services/observability
problem_type: convention
component: monitoring
severity: high
applies_when:
  - "a per-host, per-instance or per-tenant rate differs by a striking ratio and you are about to explain the ratio"
  - "a candidate explanation reproduces the headline number closely"
  - "choosing between timing tweaks for what looks like a client/server race"
  - "verifying a fix for an intermittent failure whose rate is not stationary"
  - "two independent periodic timers in a system share the same nominal period"
  - "a before/after error count is about to be offered as proof that a fix worked"
related_components:
  - telegraf_agent
  - services/observability
  - victoriametrics
  - vector_agent
tags:
  - verification
  - measurement
  - experiment-design
  - falsifiability
  - structural-verification
  - keep-alive
  - telegraf
  - victoriametrics
  - review-loop
---

# A hypothesis that fits the numbers is not thereby tested — and the better the fit, the less anyone looks

## Context

Issue #208 opened with a clean-looking measurement: over 24 h, n5pro logged **154**
`E! [agent] Error writing to outputs.http: ... /api/v1/write: EOF` and eq12 logged **2**.
Same telegraf config, same VictoriaMetrics endpoint, ~75x the error rate on one host.
The issue's own "worth checking" list proposed the natural explanations — batch size, a
proxy hop, VictoriaMetrics itself — and every one of them framed the question as *what
is wrong with n5pro?*

That framing was false, and the way it was disproved is the reusable part.

## Guidance

### 1. A hypothesis that fits the numbers is not thereby evidence

The mechanism turned out to be a keep-alive race: telegraf flushes every **60 s** and
VictoriaMetrics runs the default `-http.idleConnTimeout=1m0s`, armed when it finishes
writing each 204. Two 60-second timers, so every connection reuse arrives within a
millisecond or two of the server's idle deadline.

The obvious next question was *why does one host lose that race 75x more often?* And a
beautiful answer was available. Round-trip time to the VictoriaMetrics host, 20 pings
each: **n5pro 0.732/1.858/5.029 ms, eq12 0.041/0.050/0.069 ms** — a 37x ratio, rising to
**82x** with a full-MTU 1472-byte payload (3.785 ms vs 0.046 ms). A plausible mechanism
came with it: the server FINs the idle connection at its deadline, that FIN takes one
one-way delay to arrive, and during that flight the client may hand the doomed
connection to a new request. Window = 2D = one RTT. The asymmetry ratio should therefore
track the RTT ratio: **37–82x, against a reported 75x.**

That is an almost perfect fit, arrived at honestly, from real measurements. It was
wrong.

The danger is specifically in the quality of the fit. A hypothesis that matches the
headline number to within the measurement's own error *feels settled*, and the
felt-settledness is what suppresses the next experiment. The falsifier had been written
down before the capture — *"failing POSTs whose server-side arrival PRECEDES the
server's FIN"* — and that is the only reason it was tested at all.

### 2. Get a vantage point that can observe the mechanism, not more analysis of the same data

The discriminating instrument was a **simultaneous three-point packet capture**: the two
clients *and the server*, the last of which puts both clients' exchanges on a single
clock and removes every clock-offset argument. On the server's clock, the n5pro failure
reads:

```text
response written    1788045725.919782   -> idle deadline 1788045785.919782
doomed POST ARRIVED 1788045785.920381   = +599 us LATE
server FIN sent     1788045785.920403   = +22 us AFTER the request arrived
```

**The FIN was emitted 22 microseconds after the doomed request had already arrived.**
There was no FIN in flight for the client to have noticed. The "window equal to one RTT"
does not exist; the client had no signal at any latency. The falsifier fired.

The algebra says the same thing once you look for it, and it is worth internalising
because it is not obvious: with `P` the client's inter-request period, `proc` the
server's internal processing time and `D` the one-way delay,

```text
lateness = (request arrival) - (previous response write + 60 s) = P - proc - 60
```

`D` cancels — propagation delays the previous response and the next request equally. The
network path cannot appear in the failure condition at all. The RTT hypothesis was not
merely unsupported; it was structurally impossible, and no amount of staring at the 75x
number would have revealed that.

### 3. When the client cannot know, only eviction is a fix — that is a conclusion, not a preference

That 22 µs is what turns the fix from a menu into a single option. If the client is never
told, then no client-side *timing* adjustment — a longer timeout, a retry, a jittered
flush, a health probe — can avoid the race. The only thing the client controls is whether
it **offers** a connection old enough to be at risk.

So the fix is `idle_conn_timeout = "45s"` in `[[outputs.http]]`
(`ansible/roles/telegraf_agent/templates/telegraf.conf.j2`): evict the connection before
the server can close it. Client-side eviction is race-free by construction — Go's idle
timer takes the connection-pool mutex and skips the close if a request has already
claimed the connection — which is exactly the property the server side cannot offer. 45 s
is chosen against the observed reuse pattern (the dangerous reuses are the 50 s and 60 s
ones; the 10 s split-flush pair stays pooled), and it must stay below the server's value.

### 4. Check that the thing you are explaining is real before you explain it

Re-measured over 48 h the ratio was **7.5x**, not 75x (n5pro 508, eq12 68). The hourly
histogram showed why: the two hosts are **anti-phased**. n5pro ran 20–30 errors/h for
21 h, then sat at **exactly zero for 15 consecutive hours** — no restart, no config
change, nothing touched — then resumed. eq12 was silent for the first 18 h and then ran
1–8/h. Same-minute coincidences across the whole window: **1 out of 68**.

And in the 35-minute capture, **eq12 failed more often than n5pro** (2 errors vs 1;
server-clock lateness median +0.374 ms vs −1.286 ms). Both hosts sit permanently within
±5 ms of the same deadline; which side of it they fall on is set by a
sub-millisecond-per-minute difference between two nominally-identical 60 s timers, and
that difference drifts over hours.

The "75x" was a 24-hour snapshot of two independently drifting phases that happened to be
in opposition. Before building an explanation for a ratio, confirm the ratio is a stable
property and not a sample of something oscillating.

### 5. An epoch-driven failure rate cannot be verified by counting — verify structurally

The 15-hour zero-error stretch *before* any change is the whole argument. Any "0 errors
in 3 h after the fix" claim is indistinguishable from that stretch. Counting cannot
verify this class of fix at any observation length short of several epochs.

What does verify it is a claim about **what can happen**, not what did:

| | before | after |
| --- | --- | --- |
| max idle at any connection reuse | ~60.000 s (16/16 at-risk reuses within ±5 ms of the deadline) | **9.998 s** |
| who sends the first FIN | server, at its 60 s deadline | **client**, at 45.000742 s idle, no request in flight |
| server-close failures | 3 captured, all 3 matched to journal errors by source port | **0** |
| RST packets in capture | 8 (n5pro) / 12 (eq12) | **0 / 0** |

The post-change statement is *the dangerous state is unreachable* — the connection is
gone at 45 s, so a 60 s-deadline race has nothing to race. That is valid regardless of
how long anyone watched.

## Why This Matters

Three actors on #194 reached confident, opposite conclusions from confounded
experiments; the lesson recorded there was to design experiments that *discriminate*.
#208 is the sharper case, because the failing hypothesis here was not confounded in the
ordinary way — it was **quantitatively excellent**. It predicted a 37–82x ratio against
an observed 75x. A reviewer asking "does the evidence support this?" would have said
yes.

So "run a discriminating experiment" needs a companion rule: **numerical agreement is
not a substitute for a falsification test, and a suspiciously good fit is a reason to
test harder, not a licence to stop.** The instrument that settled it was not cleverer
analysis of the same data — it was a *new vantage point* (the server side) that could
observe the one ordering fact no client could ever see.

The cost of getting this wrong is not a wasted afternoon. The RTT story would have
produced a "fix" aimed at n5pro's network, left eq12 broken, and — because the error rate
oscillates on its own — would have looked like it worked.

## When to Apply

- A per-host, per-instance, or per-tenant metric differs by a striking ratio and you are
  about to explain the ratio. First ask whether the ratio is stable.
- A candidate explanation reproduces the headline number closely. Treat the closeness as
  a hazard, not as confirmation.
- A client/server failure looks like a race and you are choosing between timing tweaks.
  Determine whether the client ever receives a signal; if it does not, every timing tweak
  is equally useless and only eviction (or not pooling at all) can work.
- Verifying a fix for an intermittent failure whose rate is not stationary. Prefer a
  structural invariant over before/after counts.
- Two independent periodic timers in a system share the same nominal period. That is a
  standing beat-frequency hazard, not a coincidence.

## Examples

**A fit is not a test.** RTT ratio 37–82x, reported asymmetry 75x — and the server-side
capture showed the FIN leaving 22 µs *after* the request arrived, so the mechanism that
ratio appeared to confirm could not have operated at all.

**Verification: counts vs structure.**

```bash
# Weak — indistinguishable from the 15-hour zero-error epoch that preceded the change
journalctl -u telegraf --since '<apply time>' | grep -c 'Error writing to outputs.http'

# Strong — the dangerous state is unreachable. From a post-change capture, per connection:
#   max idle at reuse ......... 9.998 s   (never >= the 45 s client ceiling)
#   first FIN sent by ......... the CLIENT, at 45.000742 s idle, no request in flight
#   responses ................. all 204, zero RST
```

## Related

- [experiment-must-discriminate-between-hypotheses.md](experiment-must-discriminate-between-hypotheses.md)
  — the prior in this series. That one covers hypotheses whose predictions *overlap*, so
  the evidence selects neither. This one covers a hypothesis whose prediction was
  specific, quantitative and matched — and was still false.
- [verification-instrument-must-distinguish-fixed-from-broken.md](verification-instrument-must-distinguish-fixed-from-broken.md)
  — the same failure applied to the verification half: an error count cannot tell the
  fixed state from a quiet epoch.
- [instant-query-cannot-prove-a-series-is-live.md](instant-query-cannot-prove-a-series-is-live.md)
  — same telegraf → VictoriaMetrics path, same "a point observation is not a statement
  about a window" logic.
- Issue #208 — this diagnosis and fix.
- **Sibling exposure, recorded here because it is not separately tracked.** The same
  60 s-vs-60 s coincidence reaches two other writers to the same endpoint. Vector's
  `prometheus_remote_write` sink is *measured* hitting it (`Retrying after error` with
  `connection closed before message completed` / `Connection reset by peer`, 16 times on
  one host and 7 on the other over 48 h); it self-heals, because that sink retries the
  batch itself, so it surfaces as WARN rather than ERROR. The telegraf that runs inside
  the observability stack is *inferred* — same 60 s interval, same endpoint, no client
  idle ceiling in its config — but was never captured, and the distinction between the
  measured case and the inferred one is deliberate.

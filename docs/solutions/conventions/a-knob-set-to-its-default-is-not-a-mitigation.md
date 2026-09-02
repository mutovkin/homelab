---
title: "A knob set to its upstream default is not a mitigation — check the default before commenting a value as a fix"
date: 2026-09-01
category: conventions
module: services/immich
problem_type: convention
component: compose
severity: medium
applies_when:
  - "adding an environment variable, flag, or config key to a compose file or role and writing a comment that says what it fixes"
  - "a research report or issue names a quirk and a 'known workaround' value in the same breath"
  - "a plan's target YAML carries a comment you did not verify against the upstream source"
  - "reviewing a diff where a comment assigns a REASON to a line (VAAPI, mitigation, workaround, required by X)"
related_components:
  - immich
  - docker-compose
  - documentation
tags:
  - comments
  - defaults
  - configuration
  - review
  - immich
  - rocm
---

# A knob set to its upstream default is not a mitigation

## Context

#244 readied the latent immich ML service for ROCm (PR #248). The plan's target YAML
(`docs/superpowers/plans/2026-08-31-immich-rocm-prep.md`) and the first commit
(`e7a316e`) added this to `ansible/roles/services/immich/files/compose.yaml`:

```yaml
      # Known -rocm quirk: a loaded model holds the 890M out of its deepest
      # idle state; unload after 5 idle minutes instead of holding VRAM forever.
      MACHINE_LEARNING_MODEL_TTL: 300
```

Review flagged it. Verified 2026-08-31 against upstream
`machine-learning/immich_ml/config.py` (`model_ttl: int = 300`) and Immich's
`environment-variables.md` table ("Inactivity time (s) before a model is unloaded
(disabled if <= 0)", default `300`): **300 is Immich's own default.** The line changed
no behaviour. Worse, the ~5 minutes of elevated idle GPU draw after each inference that
the research report called a "-rocm quirk" *is* the default TTL window — the comment
presented the symptom's own duration as the cure for it.

The fix (`7655b6d`) kept the value — an explicit pin means an upstream default change
cannot silently widen that window — and rewrote the comment to say exactly what the
line is: default, made explicit, NOT a mitigation, with the verification date, the
source, and the knob to turn (lower it; never `<= 0`).

## The trap

A value that equals the default is a no-op wearing a fix's comment. Nothing catches it:
the compose parses, the container starts, the behaviour is unchanged, and the only
artefact is a sentence telling the next operator that a known problem has been handled.
On deploy day that sentence is what they read first. It was written in good faith — the
research report paired the quirk with "unload after 5 idle minutes", and the plan
copied the pairing into a comment without asking what the value would be WITHOUT the
line.

The same review found two siblings in the same change, same shape — a comment that
assigns the wrong *reason* to a correct line:

- `/dev/dri:/dev/dri  # VAAPI` on the ML container. It decodes no video; dri is the
  ROCm render node, needed *paired* with `/dev/kfd`. As labelled, it invited a future
  reader to trim an "unused VAAPI device" and break ROCm.
- The README's "Both require `/dev/dri` device access". The sentence stayed *true* — both
  containers still map the device — but its reason (VAAPI, inherited from the line above
  it) became wrong for the ML container, and it silently omitted the `/dev/kfd` the
  `-rocm` image now also needs. A true sentence with a wrong reason is still a
  wrong-reason comment: note that reviewing it for truth alone would have passed it.

A wrong-reason comment is a config change waiting to happen: it tells the next editor
which line is safe to delete.

## The rule

Before a comment says a value *fixes*, *mitigates*, *works around*, or *is required
by* anything:

1. **Look up the upstream default** for that key in the version you run (source file
   or the project's env-var table — not a blog, not the research report that
   suggested the knob).
2. **State the delta.** The comment must say what changes relative to *not setting the
   line*. If the answer is "nothing", the honest comment is "upstream default, pinned
   so a default change cannot move it silently" — or delete the line.
3. **Date and cite the check** (`Verified <date> against <path>`), the way the fixed
   comment does, so the next bump knows what to re-verify.
4. **A reason on a line is a claim; verify it like one.** Device mappings, ports,
   capabilities: if the comment names WHY the line exists, that why must be the real
   consumer, because it is the licence a future reader uses to remove the line.

Reviewers: any diff whose comment contains *quirk*, *workaround*, *instead of*,
*mitigat*, or *needed for* is a prompt to ask "what is the default?" — and to check
that a research-report value was not transcribed as a fix.

## Why This Matters

- Deploy-day operators act on role comments and READMEs before they act on upstream
  docs. A false "addressed" claim removes the problem from their list.
- Config that equals the default cannot be found by any runtime check; only a reader
  who knows the default can see it. Write that knowledge down where the line is.
- The same discipline as "measure the baseline before claiming a win"
  ([measure-the-baseline-then-verify-before-transforming.md](measure-the-baseline-then-verify-before-transforming.md)):
  a mitigation's claim needs a baseline — here, the unset value — or it is a paraphrase
  of the symptom.

## See also

- [measure-the-baseline-then-verify-before-transforming.md](measure-the-baseline-then-verify-before-transforming.md)
- [a-falsification-run-must-actually-reach-the-guard.md](a-falsification-run-must-actually-reach-the-guard.md)
  (same PR family, #240: a check that cannot fail is not a check)
- Issue #244 comment "Plan deviation, recorded before merge"; PR #248 section
  "Deviation from the plan — a comment that would have shipped a false claim"
- `ansible/roles/services/immich/files/compose.yaml`, `immich-machine-learning`
  environment block (the corrected comment)

---
title: "A guard whose accept condition is a NEGATIVE fails open: six rounds on one snapshot-location check, and the affirmative-allow inversion that ended them"
date: 2026-08-24
category: security-issues
module: observability
problem_type: security_issue
component: tooling
symptoms:
  - "a privacy guard reports the destination is safe and a 590 KB home-state readout lands in a tracked git tree, git status reporting it untracked"
  - "git rev-parse prints 'fatal: not a git repository' while standing inside a real checkout"
  - "each review round closes the exact hole the last review found and the next review finds a new one"
  - "a guard demo passes without ever exercising the branch it claims to test"
  - "a fix silently regresses a case its own predecessor caught"
root_cause: logic_error
resolution_type: code_fix
severity: high
related_components:
  - reconcile-ha-entities
  - home-assistant
  - git
tags:
  - fail-open
  - fail-closed
  - allow-list
  - guard-design
  - path-canonicalization
  - git-worktree
  - threat-model
  - privacy
---

# A guard whose accept condition is a negative fails open

## Context

`scripts/reconcile-ha-entities.py` captures Home Assistant's `/api/states` to a
snapshot file so a run can be replayed byte-for-byte. That payload is a full
readout of a house — occupancy, presence, device names — so the script carries a
guard whose only job is to keep the file out of a tracked git tree, where a stray
`git add -A` would publish it.

That guard took **six review rounds**. Each round closed exactly the hole the
previous review had found, and the next review found a new one. The rounds are
worth reading as a sequence, because the sequence is the lesson: every fix was
correct about the hole it patched, and every fix left the guard fundamentally
unchanged.

The failure it exists to stop is not hypothetical. It happened, twice, during
review:

```
round 1:  /Users/surge/dev/homelab/scratch171/ha.json   accepted; 590 KB home-state
          readout written into the main checkout; `git status` reported it untracked
round 3:  a reviewer reproduced a 601 KB home-state write into an enclosing
          checkout, exit 0
round 5:  <approved-root>/absent/../victim_checkout/ha-states-demo.json accepted;
          128 bytes written inside the checkout; `git status: ?? ha-states-demo.json`
```

## Guidance

**A guard whose ACCEPT condition is a negative fails open by construction.**

"Write here if nothing in this location looks like a checkout" is not a safety
property. It is an enumeration of forbidden shapes, and an enumeration is only as
good as its list. It fails OPEN on the shape nobody enumerated — which is always
the next one, because the reason it is not on the list is that nobody thought of
it.

Three patches to the list, and then a fourth hole the list could never have
caught, is the signature. When a guard needs a fourth patch to the same question,
stop patching the list and invert the question. (Rounds 1-3 each extended the
enumeration; round 4 found a shape with nothing to enumerate, which is what forced
the inversion, and round 5 showed the inverted gate could still be applied to the
wrong string.)

**The fix shape that terminated the loop**, all four parts required:

1. **Affirmative allow.** The write is permitted only under a positively approved
   root — never merely "not recognised as forbidden". In this script:
   `$TMPDIR`, `/tmp`, `/private/tmp`, or a directory the operator names with
   `--snapshot-allow-root` (`scripts/reconcile-ha-entities.py:561`,
   `allow_roots()`). An unlisted root is a refusal, so an unanticipated layout
   fails closed instead of open.
2. **Evaluated on the CANONICAL path.** Canonicalize once, first, and let every
   layer consume only that form (`canonical_path()`, line 625; the call site
   canonicalizes before any decision at line 860). A positive gate applied to the
   wrong string is a negative gate in disguise — see round 5 below.
3. **Every error refuses.** Any ambiguity, unreadable ancestor, unlisted root, or
   failed probe is a refusal, not a shrug. Subprocess evidence may only WIDEN the
   forbidden set; it is never allowed to be the thing that says yes
   (`git_worktrees_containing()`, line 788: "a git answer of 'not a repository'
   means only 'git adds no extra roots' — never 'safe'").
4. **A DECLARED threat model with named residuals.** This is what let review
   terminate. The docstring states the guard is *accident prevention* for a tool
   the operator runs on their own machine, aimed at the failure that actually
   happened, and explicitly *not* a defence against an adversary who controls the
   arguments, the environment, or the filesystem — "anyone who does has already
   won by simpler means than a crafted path". It then names four residuals it
   does not chase: `core.worktree` in a bare repo pointing into an approved root,
   `PATH`/`GIT_CONFIG_*` suppressing the probes' widening, TOCTOU between check
   and write, and any resolution case requiring deliberately hostile input.

Without part 4 the loop does not end. A guard with no stated scope has no
definition of "done", so every round can always find one more shape. Round 6
returned APPROVE-WITH-RESIDUALS from both reviewers precisely because every
residual they found was already named.

## Why This Matters

The patched rounds were not sloppy work. Each was a correct fix to a real defect
-- three of them reproduced end to end as writes into a tree, the fourth read out
of the code path rather than reproduced. That is what makes this worth writing down: **correctness of
each patch is not evidence that the design is converging.** The guard was
converging on a longer list, not on a safer property.

The economics are lopsided in a way that is easy to miss. An enumeration must be
right about every shape; an allow-list must be right about one. Under an
enumeration the cost of a shape nobody imagined is a silent write of personal
data into a tracked tree. Under an allow-list the cost is a refusal the operator
fixes with one flag.

The same disease shows up in guards that are *tautologies* — a check that cannot
fail is a negative condition taken to its limit:

- **P2** in this same script was labelled "the one that CAN fail on data". It
  could not. The seen set is a union of both endpoints, so a dated entity can
  never appear in the never-seen list, for any input. One reviewer brute-forced
  **200,000 randomized inputs without producing a failure** (a reviewer's
  figure, reported during review; no fixture for it exists in the tree). It is now labelled a
  union-code canary — it catches a source edit, and it is honest about catching
  nothing else.
- **`failed_when: false`** does not "let a result through". It *assigns*
  `failed: False`, so a paired `assert: <reg> is not failed` is literally
  `assert: true`. Measured against a `wait_for` that timed out: `failed_when:
  false` yielded `failed=False` and the assert passed; `ignore_errors: true`
  yielded `failed=True` and the assert fired. One such guard sat inert in this
  repo for its whole life while claiming to prove a host log stream was live.

Related: [verification-instrument-must-distinguish-fixed-from-broken](../conventions/verification-instrument-must-distinguish-fixed-from-broken.md) -- see Related below.

## When to Apply

Reach for the inversion when any of these hold:

- A guard's accept path is written as "no evidence of X was found".
- The guard has been patched more than twice for the same question.
- The evidence the guard depends on comes from a subprocess, an environment
  variable, or anything else the surrounding system can influence.
- The cost of a false accept is irreversible (data published, a secret written, a
  destructive command run) while the cost of a false refuse is a flag.

Do NOT reach for it when the set of allowed values is genuinely unbounded and the
failure is cheap — an allow-list you cannot enumerate is just a different way to
be wrong, and it will be worked around.

Whichever shape you choose, **declare the threat model in the code**. It is the
difference between a review that terminates and a review that recurs.

## Examples

### Round 1 — the empty forbidden set

The guard asked git about the destination directory. `git -C <nonexistent>` exits
128, and the failure path returned an empty list, silently degrading the guard to
the script's own repo root.

```
git -C /Users/surge/dev/homelab/scratch171 rev-parse --show-toplevel
  -> rc 128, no stdout
  -> forbidden roots = []          (should have been: the enclosing checkout)
```

`mkdir(parents=True)` then CREATED the missing directory inside the tracked tree
and a **590 KB** home-state readout landed in it. `git status` in the main
checkout reported it untracked. The comment recording this still sits at
`scripts/reconcile-ha-entities.py:815-820`.

### Round 2 — non-zero read as "safe"

Any other non-zero git exit — dubious ownership, a corrupt `.git`, a permission
error — was also read as "not a repository, therefore safe", with no message at
all. A privacy guard that fails open *silently* is worse than the static test it
replaced, which at least could not do that.

### Round 3 — git says "not a git repository" from inside a repository

The accept arm matched git's stderr. Real git prints the canonical line while
standing inside a tracked tree under **seven measured conditions**:

```
dangling worktree .git-file gitdir     fatal: not a git repository: (null)
.git file, missing relative gitdir     fatal: not a git repository: <path>
.git/HEAD deleted                      fatal: not a git repository (or any of the parent directories): .git
.git/objects deleted                   same canonical line
mode-000 .git                          same canonical line
GIT_DIR=/nonexistent (inherited)       fatal: not a git repository: '/nonexistent'
GIT_CEILING_DIRECTORIES=<repo>         same canonical line
```

A reviewer reproduced an end-to-end **601 KB** home-state write into an enclosing
checkout with exit 0. The fix made the primary refusal a pure-filesystem ancestor
walk and demoted git to a widen-only role — correct, and still an enumeration.

### Round 4 — a work tree with no `.git` anywhere in the ancestry

An **extrinsically defined** work tree exists only in the environment: a bare repo
plus an exported `GIT_DIR`/`GIT_WORK_TREE` (the dotfiles pattern — and the exact
environment git exports into every hook, `rebase --exec`, `bisect run` and
`submodule foreach`), or `core.worktree` set in a bare repo's config.

```
git init --bare  $S/bare.git
GIT_DIR=$S/bare.git GIT_WORK_TREE=$S/wt  git -C $S/wt rev-parse --show-toplevel
  -> $S/wt                                            (rc 0: it IS a work tree)
env -u GIT_DIR -u GIT_WORK_TREE git -C $S/wt rev-parse --show-toplevel
  -> fatal: not a git repository (or any of the parent directories): .git
```

There is no `.git` in the destination's ancestry, so the filesystem walk passes
cleanly. Measured in this session: the pre-fix module **ACCEPTED** it and
replayed. Round 3's env sanitization — added so an inherited `GIT_DIR` could not
blind the probe — had made the probe blind to the environment that *defines* the
work tree, deleting a detection an earlier revision had by accident. The fix runs
two probes, sanitized and inherited, both widen-only (line 796, "TWO probes, and
neither is redundant — do not delete one").

### Round 5 — the positive gate on a non-canonical path

The guard had been inverted to affirmative allow, and it was still bypassed —
because the gate was evaluated on a string the kernel would never see. A
hand-rolled canonicalizer resolved the deepest *existing* ancestor and re-joined
the remaining tail verbatim, so a `..` after a nonexistent component survived.
`Path.parents` treats `..` as an ordinary name:

```
literal   <approved-root>/absent/../victim_checkout/ha-states-demo.json
gate      Path('/private/tmp') in path.parents        -> True   (matched the pre-`..` prefix)
.git walk lstat of <literal>/.git at each ancestor    -> ENOENT everywhere -> None
OS truth  os.path.realpath(literal)                   -> <approved-root>/victim_checkout/...
result    mkdir(parents=True) created `absent`, the kernel collapsed the `..`,
          128 bytes written INSIDE the checkout, `git status: ?? ha-states-demo.json`
```

A positive gate applied to the wrong string **is a negative gate in disguise**: it
proves something true of a path nobody will ever write to. The fix is
`os.path.realpath` once, first, feeding all three layers — it resolves symlinks in
the existing prefix and collapses `..`, including after a component that does not
exist — plus a hard refusal for any `..` that survives
(`scripts/reconcile-ha-entities.py:625-663`). The docstring records it as a bitter
lesson: *do not hand-roll path canonicalization*; the helper written to close hole
four opened hole five, and it did so precisely because it was written to make the
affirmative proof go through.

### The verification discipline that actually caught these

**Every guard demo must be shown to FAIL against the live defect before its pass
counts.** This branch is the argument for that rule, because it produced two demos
that passed while testing nothing:

- A "git is unrunnable" demo used a **non-executable** shim. POSIX PATH search
  skips a non-executable file and keeps searching, so real git ran; the demo then
  counted an unrelated connection error as its refusal. Rewritten to remove every
  git-holding directory from `PATH` while keeping the rest (`command -v git ->
  None`, `command -v ansible-vault` still resolving), it exercised the intended
  arm.
- A `$HOME` unlisted-root demo printed "REFUSED" for the pre-fix module. The
  guard had ACCEPTED; the refusal came from a closed port on the stub HA URL. The
  demo now labels that case `guard ACCEPTED (fell through to a live HA fetch;
  only the closed port stopped it)`.

And one fix regressed a case its predecessor caught — round 3's sanitization
deleted round 2's accidental detection of env-defined work trees (round 4 above).
A demo matrix that only ever runs forward will not catch that; re-running the full
prior matrix after every rewrite will.

For a branch that is unreachable today, force it. The belt-and-braces `..`
refusal cannot fire while `realpath` collapses `..` on POSIX, so it was exercised
by stubbing `os.path.realpath` to return a `..`-bearing path and confirming the
named `Failure`:

```
forced (realpath stubbed to return a `..` path): REFUSED --
  refusing to use the HA snapshot path <p>: it still contains a '..' component
  after canonicalization (<q>). Every location check would then be reading a
  different path from the one the kernel writes to.
```

A guard nobody has seen fail is not a guard, whether it is unreachable by
construction or merely untested.

## Related

- [diff-leaks-vaulted-secrets-and-empty-auth-vars-disable-auth](diff-leaks-vaulted-secrets-and-empty-auth-vars-disable-auth.md)
  -- same category, same shape one layer down: an empty/negative default read as
  "permitted" instead of refusing, and the canary recipe for proving a test can fail.
- [verification-instrument-must-distinguish-fixed-from-broken](../conventions/verification-instrument-must-distinguish-fixed-from-broken.md)
  -- the parent thesis: a guard is evidence only if its output differs when the
  thing is broken.
- [grafana-datasource-version-gate-freezes-rotated-secret](../integration-issues/grafana-datasource-version-gate-freezes-rotated-secret.md)
  -- **owns the `failed_when: false` tautology material**, with the measured A/B.
  Cited here rather than restated; this doc's new content is the negative-accept
  -condition thesis and the six-round escalation.
- [lxc-features-nfs-invalid-key-silent-green](../integration-issues/lxc-features-nfs-invalid-key-silent-green.md)
  -- prior art for the round-1 arm: a swallowed non-zero exit plus a marker
  reporting intent rather than outcome.
- [docker-port-allowlist-bridge-consumers-and-peer-auth](../conventions/docker-port-allowlist-bridge-consumers-and-peer-auth.md)
  -- the repo's existing affirmative-allow-list-as-a-no-defaults-contract precedent.
- [nftables-input-hook-inert-for-docker-published-ports](../integration-issues/nftables-input-hook-inert-for-docker-published-ports.md)
  -- verify from a BLOCKED source, not just an allowed one: the negative test these
  six rounds kept re-learning.
- [ansible-change-loop-pitfalls](../conventions/ansible-change-loop-pitfalls.md)
  -- an existence gate can defeat the verification it precedes; a skipped branch is
  an untested branch.

Disambiguation: [scoped-nftables-on-live-host](../conventions/scoped-nftables-on-live-host.md)
describes a deliberately **fail-open** nftables table. That is not this doc's
"fails open": there it names the lockout posture of a live-host firewall change (an
unloaded table must not sever SSH), a considered trade-off. Here it names a guard
that permits what it exists to forbid.

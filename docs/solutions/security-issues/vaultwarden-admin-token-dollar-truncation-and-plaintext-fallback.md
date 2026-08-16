---
title: "An unquoted $ in a templated .env truncates the secret, and Vaultwarden accepts the wreckage as a plaintext admin token"
date: 2026-08-16
category: security-issues
module: services/vaultwarden
problem_type: security_issue
component: authentication
symptoms:
  - "docker compose config resolves an unquoted ADMIN_TOKEN=$argon2id$v=19$m=65540,t=3,p=4$<salt>$<hash> to the string =19=65540,t=3,p=4, exit 0"
  - "The only protest is a per-segment stderr warning — 'The \"argon2id\" variable is not set. Defaulting to a blank string.' — that nothing in the deploy is gated on"
  - "Any templated .env value containing $ (SMTP/app passwords, generated secrets, PHC hashes) is truncated at deploy time without failing anything"
  - "A truncated PHC hash loses its $argon2 marker, so Vaultwarden stops treating it as a hash and compares it as a literal plaintext token"
  - "The container starts, stays healthy, and the deploy reports green while /admin is guarded by a short plaintext credential visible in docker inspect"
  - "The only upstream signal is a [NOTICE] line on container stdout that no deploy step reads"
root_cause: missing_validation
resolution_type: code_fix
severity: critical
related_components:
  - vaultwarden
  - docker-compose
  - services/_deploy
  - ansible-vault
  - env-templating
tags:
  - vaultwarden
  - docker-compose
  - dotenv
  - secrets
  - argon2
  - env-templating
  - ansible
  - silent-failure
---

# An unquoted `$` in a templated `.env` truncates the secret, and Vaultwarden accepts the wreckage as a plaintext admin token

## Problem

Hardening Vaultwarden for #81 replaced the plaintext `ADMIN_TOKEN` with an Argon2id PHC
hash, so that `docker inspect`, `printenv`, the on-disk `.env`, and every backup of them
hold a value that can *verify* an admin login but never *produce* one. A PHC string is
`$`-delimited by construction (`$argon2id$v=19$m=65540,t=3,p=4$<salt>$<hash>`), and that
collides with a compose behaviour there is no reason to anticipate: **Docker Compose's
dotenv parser interpolates `$VAR` inside unquoted values.** Written unquoted, the hash is
truncated to a fragment. Compose does say something — a stderr warning per eaten segment —
but nothing fails: exit 0, config valid, container healthy, deploy green.

That would be a self-limiting bug — a broken hash means a broken admin login — except for
the second half. **Vaultwarden only treats `ADMIN_TOKEN` as a hash when it starts with
`$argon2`; anything else is compared as a literal plaintext token.** Truncation destroys
the `$argon2id` segment first, so the mangled value does not fail closed. It becomes a
working plaintext admin credential sitting in `docker inspect` output — *exactly* the
exposure #81 existed to remove.

The two traps compose. Trap A is a mechanism that reliably manufactures the malformed
value that Trap B then accepts. Either alone is a nuisance; chained, they turn a security
fix into a security regression that is indistinguishable from success.

**Provenance, so the evidence is not overstated:** the truncation was caught on a
`docker compose config` fixture *before* anything shipped, and the quoting was in place
from the first deploy of the hash — the corrupted value never reached the live host.
Trap B is established from Vaultwarden's source (quoted below), not from a live
observation of a mangled token being accepted.

## Symptoms

- A `.env` line `ADMIN_TOKEN=$argon2id$v=19$m=65540,t=3,p=4$<salt>$<hash>` (unquoted)
  resolves through `docker compose config` to `=19=65540,t=3,p=4`: `$argon2id`, `$v`,
  `$m`, `$<salt>` and `$<hash>` are read as undefined variable references and expand to
  empty strings. The same value single-quoted resolves to the intact literal.
- The only protest is a warning nobody is gated on. Compose emits one stderr line per
  eaten segment — `WARN The "argon2id" variable is not set. Defaulting to a blank
  string.`, and the same for `v`, `m`, the salt and the hash — then exits 0.
  `docker compose up` succeeds, the container passes its `/alive` healthcheck
  (`ansible/roles/services/vaultwarden/files/compose.yaml:51-56`), and Ansible's
  `docker_compose_v2` does not turn that warning into a failure, so the deploy is green.
- The resulting value no longer starts with `$argon2`, so `/admin` authenticates it by
  literal string comparison — the hardening is undone, and the surface is *worse* than
  before, because the credential is now short and structureless rather than a real
  generated token.
- The one upstream signal is a startup `[NOTICE] You are using a plain text ADMIN_TOKEN
  which is insecure.` printed to stdout. It is not an error, not a healthcheck failure,
  and not written to the configured `LOG_FILE` (`compose.yaml:39`) — and no deploy step
  reads container stdout.
- In `docker compose config` output a literal `$` is rendered `$$`, which at a glance
  reads like a second corruption stacked on the first.

## What Didn't Work

### (a) Assuming a mangled secret would surface as a *failure*

The instinct is that a value containing shell-special characters either works or blows up.
Dotenv has no such contract: `$` inside an unquoted value is a *documented* interpolation
trigger, and an undefined variable expands to the empty string rather than raising. The
file parses, the schema validates, the resolved config is well-formed, exit code 0.

The subtler trap is that there *is* a signal, and it still does not help. Compose prints
`WARN The "argon2id" variable is not set. Defaulting to a blank string.` — once per eaten
segment — to stderr. That line is true, specific, and completely ineffective: it is a
warning rather than an error, Ansible's `docker_compose_v2` does not fail on it, and it
scrolls past inside a multi-service deploy that nobody reads line-by-line when the recap
says `failed=0`. This is the more useful shape of the lesson than "it was silent": the
information existed and no gate consumed it. The only reliable way to see the problem is
to look at the *resolved value* — `docker compose config` on a fixture, quoted versus
unquoted, side by side.

### (b) Assuming quoting only mattered for the token

The first fix quoted `ADMIN_TOKEN` and stopped. That fixed the instance and left the
class. The PHC hash was merely the value that *made the behaviour visible*, because it is
`$`-delimited every time; the parser does not care which variable it is looking at.
`SMTP_PASSWORD` (`ansible/roles/services/vaultwarden/templates/env.j2:21`) is the obvious
next casualty — app passwords and generated secrets routinely contain `$`, and the failure
there is mail quietly not authenticating weeks after the vault edit that caused it, with
nothing connecting the two. The rule had to widen from "quote the hash" to **quote every
templated value**.

### (c) A near-miss: the right check existed, aimed at the wrong character (session history)

This was not a new idea. An earlier Watchtower change (#71) templated a Shoutrrr SMTP URL
containing `&` and a Gmail app password with `%20`-encoded spaces, and explicitly did not
trust that those survived the dotenv parser: it verified by inspecting the *running
container's* environment, then forced a real SMTP AUTH attempt so a mangled credential
would log an error. Both passed. The verification shape was already invented and already
proven — it was applied to `&` and percent-encoding and never generalized to `$`. A
one-off "does this survive?" check catches the character you were worried about; only a
blanket rule catches the one you were not.

### (d) Reading `$$` in `docker compose config` output as evidence the value was still wrong

After single-quoting, `docker compose config` prints the token with every `$` doubled.
This looks like an over-correction. It is not: that output is itself a valid compose file,
so `config` escapes literal dollars as `$$` for round-trip safety. It is *display*
escaping, not the runtime value. Settle it with a stable re-parse rather than by squinting
— feed the emitted config back through `docker compose config` and the output is
unchanged, whereas a genuinely doubled value would grow another round of doubling.

### (e) Trusting the consumer to reject a wrong-shaped credential

It would be natural to assume an application whose config field is documented as "an
Argon2 PHC hash" validates that it received one. Vaultwarden's behaviour is more subtle
than either "validates" or "doesn't", and the subtlety is what makes this dangerous — see
below.

## Solution

Two independent guards, one per trap, both in the vaultwarden role.

**1. Single-quote every templated value in the `.env` (Trap A).**
`ansible/roles/services/vaultwarden/templates/env.j2:1-9` carries the reasoning as a
header comment so the next editor does not "clean up" the quotes:

```jinja
# Every value is single-quoted, deliberately. Compose's dotenv parser interpolates
# $VAR inside UNQUOTED values, so any secret containing a `$` is silently truncated
# rather than rejected — the ADMIN_TOKEN hash below is just the instance that made
# this visible. Single-quoted values are taken literally, and the result is not
# re-expanded by the compose file's ${VAR} interpolation.
# Caveat: dotenv single-quoted strings have NO escape for a single quote, so a value
# that itself contains `'` cannot be represented here. None do today.
```

Every assignment from `env.j2:10` through `:25` follows `KEY='{{ var }}'` — twelve
assignments, eleven of them vault values (the twelfth, `VAULTWARDEN_NETWORK_SUBNET`, comes
from host_vars) — including the ones that "cannot" contain a `$` today.

The caveat that travels with the rule concerns the single quote itself. Jinja emits the
value unescaped, so a secret containing `'` terminates the quoted string early. That case
**fails loudly**, which is the one piece of luck here: compose aborts with
`failed to read .env: line 1: unexpected character "'" in variable name` and exit 1,
rather than mangling anything. (compose-go *does* honour a backslash-escaped `\'` inside
single quotes — the escape branch is quote-type agnostic — but nothing in this pipeline
produces one, so the practical rule stands.) A secret containing `'` therefore needs a
different transport, not a cleverer quoting trick; it cannot silently slip through. Per
the implementing change, none of the eleven current vault values contains `'` or a
backslash.

**2. Assert the PHC shape before anything else runs (Trap B).**
`ansible/roles/services/vaultwarden/tasks/main.yml:72-82`:

```yaml
- name: Assert the Vaultwarden admin token is an Argon2id PHC hash
  ansible.builtin.assert:
    that:
      - vault_vaultwarden_admin_token_hash is match('^\$argon2id\$')
      - vault_vaultwarden_admin_token_hash is match('^[A-Za-z0-9+/$,=.]+$')
    fail_msg: >-
      vault_vaultwarden_admin_token_hash is not an Argon2id PHC string, so Vaultwarden
      would treat it as a PLAINTEXT admin token and undo #81. Regenerate it with
      `docker run --rm -it vaultwarden/server:latest /vaultwarden hash` and store the
      $argon2id$... value itself (not the ADMIN_TOKEN='...' wrapper the helper prints).
    quiet: true
```

Four deliberate properties:

- **Two conditions, not one.** The prefix match catches "this is not a hash at all"; the
  charset match catches contamination — a truncated fragment, an inner `'`, or trailing
  whitespace from a hand-edit. (One gap, verified by probe: Python's `$` matches before a
  final newline, so a single trailing `\n` passes the charset test. Low severity — such a
  value still starts with `$argon2`, so it lands in Vaultwarden's *loud* branch and aborts
  startup rather than degrading to plaintext.)
- **Placement.** It sits ahead of every task that touches the container or the vault data
  (`docker_container_info` at `ansible/roles/services/vaultwarden/tasks/main.yml:90`, the
  backup block at `:188`). A bad token must stop the play before the role starts stopping
  containers.
- **It runs in check mode.** A pure variable check with no host interaction, so `--check`
  catches a bad vault edit during the dry run.
- **It never prints the value.** `quiet: true` suppresses the per-condition echo, and the
  `fail_msg` explains the consequence and the remediation without interpolating the
  variable. Verified by negative proof: with a deliberately bad value the play fails at
  that task (`failed=1`) before any other task runs, and the output contains only the
  assertion expression and the variable *name*.

**Recreate-safety proof.** Reformatting a live `.env` looks like it should bounce the
container, and for a password vault a needless restart is not free. It does not:
`docker compose config --hash=vaultwarden` over the real compose file produced an
**identical service config hash** for the old-format and new-format `.env` — reproducible
anywhere. Observed additionally during the live run: that hash equalled the
`com.docker.compose.config-hash` label on the already-running container. Compose's
recreate decision compares the *resolved* service config, not the `.env` bytes.

## Why This Works

The two guards are independent because the two failures are independent.

**Quoting fixes the transport.** A single-quoted dotenv value is literal — the parser does
no interpolation inside it — and the compose file's own `${VAR}` substitution
(`compose.yaml:29` for `ADMIN_TOKEN`) is **single-pass**: it substitutes the value and does
not re-scan the result for further `$` references. Both halves matter, and together they
close the hole rather than narrowing it.

**The assert fixes the consumer-trust gap** — and Vaultwarden's actual behaviour is why
that gap exists. Two places in the upstream project
([dani-garcia/vaultwarden](https://github.com/dani-garcia/vaultwarden), paths below are
upstream, not in this repo) decide it. Its `src/config.rs` validates at startup:

```rust
Some(t) if t.starts_with("$argon2") => {
    if let Err(e) = argon2::password_hash::PasswordHash::new(t) {
        err!(format!("The configured Argon2 PHC in `ADMIN_TOKEN` is invalid: '{e}'"))
    }
}
Some(_) => {
    println!("[NOTICE] You are using a plain text `ADMIN_TOKEN` which is insecure. ...")
}
```

and its `src/api/admin.rs` decides each login the same way:

```rust
Some(t) if t.starts_with("$argon2") => { /* argon2 PHC verify */ }
Some(t) => crate::crypto::ct_eq(t.trim(), token.trim()),
```

The dispatch is on the **`$argon2` prefix alone**. This produces an inverted severity
curve that is the sharpest thing in this whole learning:

- A **partially** corrupted hash that still begins with `$argon2` hits the first branch,
  fails `PasswordHash::new`, and **aborts startup loudly**. You cannot miss it.
- A **completely** corrupted hash — which is exactly what `$`-interpolation produces,
  because `$argon2id` is the first segment eaten — falls into the plaintext branch and is
  accepted, with only an unread stdout `[NOTICE]` to mark it.

So the failure is silent *because* the corruption was thorough. The mechanism that
mangles the value most destroys precisely the marker that would have made the mangling
fatal. There is no configuration to turn the fallback off, which is why no amount of care
in the templating layer removes the need for our own shape check.

And this is the generalizable shape, which is why it earns a doc rather than a comment:
**when a config value is a hashed or encoded credential, two independent things must hold
— the transport must preserve it byte-exactly, and the consumer must reject a wrong-shaped
value.** The second is not under your control and frequently is not true. "Interpret an
unparseable credential as a literal one" is a common, well-intentioned fallback. Assert
the shape yourself, at deploy time, rather than waiting for a complaint the application is
never going to make.

## Prevention

- **Single-quote every templated value in an Ansible-generated `.env`, not just the ones
  you think need it.** Which secrets contain a `$` is a property of the *vault contents*,
  which change without touching the template — so the only stable rule is "all of them."
  Carry the caveat with the rule: a value containing `'` closes the quoted string early
  and makes compose abort with `unexpected character "'" in variable name` — loud, and so
  the acceptable failure; that value needs a different transport. Note this deliberately
  overrides the repo's YAML convention ("quote strings only when required") — that rule
  governs YAML, not dotenv.
- **Do not read "there was no error" as "there was no signal."** Compose warned on stderr
  for every truncated segment and the deploy was still green, because no step was gated on
  the warning. When a tool's only complaint is a warning, either gate on it or replace it
  with an assertion of your own; an ungated warning is indistinguishable from silence at
  the point where it matters.
- **Assert the shape of any hashed or encoded credential at deploy time.** Do not assume
  the consumer validates it. Anchor on both a format prefix and the full expected charset,
  so truncation and contamination are both caught.
- **Check what a permissive consumer does with a *completely* wrong value, not just a
  slightly wrong one.** Graceful degradation is usually keyed off a marker at the start of
  the string, so the more thoroughly a value is corrupted, the more likely it slips into
  the permissive branch. Partial corruption is the safe case; total corruption is the
  dangerous one.
- **Put shape asserts before any destructive or data-touching task**, and keep them free
  of host interaction so they also fire under `--check`. An assert that runs after the
  role stopped a container has already cost you the thing it was protecting.
- **Never let a secret assert print its subject.** Use `quiet: true` and a `fail_msg` that
  gives the consequence and the remediation command without interpolating the value;
  failure output lands in CI logs and pasted scrollback. (Related: #88 — the same env
  template prints secrets on *both* sides of `--diff`.)
- **Verify a secret survived the pipeline by inspecting the resolved value, not by
  checking that the service started.** `docker compose config` on a fixture is the cheap
  version; `docker inspect` on the running container is the authoritative one. "The
  container is healthy" is fully consistent with the credential having been destroyed or
  replaced by a plaintext one. When this check is invented for one awkward character,
  generalize it to all of them immediately (session history: it was built for `&` and
  `%20` in #71 and not reused here).
- **Read `$$` in `docker compose config` output as display escaping, not corruption**, and
  settle it with a stable re-parse. Otherwise you will "fix" a correct value and
  reintroduce the original bug.
- **Prove a formatting-only change is resolution-neutral before landing it on a live
  service.** `docker compose config --hash=<svc>` under old and new inputs, compared to the
  running container's `com.docker.compose.config-hash` label, answers "will this recreate
  anything?" definitively.

## Related Issues

- **#81** — Vaultwarden hardening: the Argon2id `ADMIN_TOKEN` and the tcp/8086 firewall
  scoping. Both traps were found here; the enforced controls are written up in
  `ansible/roles/services/vaultwarden/README.md`.
- **#107** — data-gated pre-upgrade backups, implemented in the same branch. The same
  discipline elsewhere: gate on the thing that actually matters (the vault data), and
  refuse to proceed when the probe cannot be trusted rather than reading an ambiguous
  answer as the safe one.
- **#117** — follow-up: fleet-wide `.env` quoting sweep. Every service role templates its
  `.env` through the same shared `services/_deploy` task, so the unquoted-value exposure
  is a whole-fleet property. Only vaultwarden's template is fixed today.
- **#88** — secrets printed on both sides of `--diff` for a secret-bearing env template.
  The disclosure half of the same env-template surface.
- [nftables-input-hook-inert-for-docker-published-ports](../integration-issues/nftables-input-hook-inert-for-docker-published-ports.md)
  — the other half of #81, and the same lesson in a different subsystem: a control that
  loads cleanly, validates cleanly, and protects nothing.
- [vector-057-silent-log-pipeline-failure](../integration-issues/vector-057-silent-log-pipeline-failure.md)
  — the standing rule this instantiates: assert the *effect*, not that the process is
  running. Note the mirror-image detail: compose interpolating `${VAR}` inside the compose
  file is what *saved* Vector's sibling services, while compose interpolating `$VAR` inside
  the `.env` is what breaks this one — so that doc's scoping of interpolation to the
  compose file should not be read as "the `.env` is a safe passthrough".
- [postgresql-mounted-configs-never-deployed-or-read](../integration-issues/postgresql-mounted-configs-never-deployed-or-read.md)
  — the file-level twin: "mounted is not wired" versus "templated is not received intact".
- [unattended-upgrades-silently-inert-fleet-wide](unattended-upgrades-silently-inert-fleet-wide.md)
  — the same silent-green security-control class, and the source of the "assert the
  artifact, not the proxy" rule this instantiates.
- [ansible-change-loop-pitfalls](../conventions/ansible-change-loop-pitfalls.md) —
  check-mode honesty and the "a skipped branch is an untested branch" rule that governs
  how the PHC assert must behave.

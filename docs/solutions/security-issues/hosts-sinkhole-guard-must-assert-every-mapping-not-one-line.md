---
title: "A hosts-file loopback sinkhole guard must assert the host's whole resolution set, not that the sinkhole line exists"
date: 2026-11-19
category: security-issues
module: services/navidrome
problem_type: security_issue
component: tooling
symptoms:
  - "The deploy guard 'a 127.0.0.1 <analytics-host> line exists in the container /etc/hosts' stayed green on hosts files where the plugin's analytics could still reach the real endpoint"
  - "A second mapping for the same host (before or after the sinkhole line, IPv6, or as an alias column) defeats the fence: Go's hosts lookup returns addresses from EVERY line naming the host, and the dialer falls through to the next address when Navidrome's plugin netguard refuses loopback"
  - "Uppercase or trailing-dot spellings of the host (Go matches names case-insensitively and treats `host.` and `host` alike) were invisible to a case-sensitive exact-token grep"
  - "A '*' or IP/CIDR entry in the plugin manifest's http allowlist is the only netguard bypass of a loopback refusal, so the fence silently depends on an allowlist nobody asserted exactly (and websocket carries its own host list)"
root_cause: logic_error
resolution_type: code_fix
severity: high
framework_version: "navidrome 0.64.2"
related_components:
  - navidrome
  - docker-compose
  - extra-hosts
  - plugin-netguard
  - go-net-resolver
tags: [guard-design, fail-open, allow-list, privacy, etc-hosts, dns-sinkhole, fault-injection, navidrome]
---

# A hosts-file loopback sinkhole guard must assert the host's whole resolution set, not that the sinkhole line exists

## Problem

The #347 change installs a pinned Musixmatch lyrics plugin (a WASM plugin) into
Navidrome 0.64.2. The plugin ships two analytics settings that default to ON and post
to the maintainer's endpoint. No admin setting narrows the plugin's HTTP reach, so the
change fences that endpoint at name resolution. Compose maps the analytics host to
loopback inside the container
(`ansible/roles/services/navidrome/files/compose.yaml:105-106`):

```yaml
    extra_hosts:
      - "${NAVIDROME_PLUGIN_ANALYTICS_HOST:?required}:127.0.0.1"
```

The fence does not rely on loopback being unreachable. It relies on Navidrome's plugin
netguard (`plugins/host_netguard.go` in navidrome/navidrome v0.64.2, upstream source), which refuses a dial to a NAMED host whose
resolved address is loopback or private. Only a `*` entry or a literal IP/CIDR in the
manifest's `requiredHosts` bypasses that refusal.

The first version of the guard, in the #347 change, checked that the sinkhole line was present:

```yaml
      - >-
        navidrome_container_hosts.stdout_lines | map('split') | select
        | selectattr('0', 'equalto', '127.0.0.1')
        | selectattr('1', 'equalto', navidrome_musixmatch_analytics_host)
        | list | length == 1
```

The guard asked "is there exactly one `127.0.0.1 <host>` line?". The question that
matters is "what can the host resolve to?", and review found the guard passed states
where analytics would leave the container.

## Symptoms

Nothing failed. Every state below kept the deploy green with analytics able to escape:

- A second mapping for the host, before or after the sinkhole line
  (`203.0.113.7 <host>`). The sinkhole line still exists exactly once, so the count
  stays 1.
- The host as an alias in a later column (`203.0.113.7 other.example <host>`). The old
  guard only read column 1.
- An IPv6 line naming the host.
- An uppercase or trailing-dot spelling (`<HOST>`, `<host>.`) on a non-loopback line.
  `equalto` is exact, so the guard never saw it.

## What Didn't Work

- **Counting the sinkhole line.** Go's hosts-file lookup (the image's `nsswitch.conf`
  reads `hosts: files dns`) returns the addresses from EVERY line that names the host,
  in any alias column, case-insensitively, with `host.` and `host` treated alike (Go appends the dot to every
  dotted name). The dialer tries the addresses of one family in order and races the
  other family as a fallback. When the netguard refuses the loopback one, the dial
  moves on to the next. So any extra mapping anywhere in the file is a route out, and
  "the sinkhole line is there" says nothing about it.
- **Matching column 1 with `equalto`.** This is the same blind spot in a smaller form.
  The resolver's matching rules (any column, case, trailing dot) are wider than the
  guard's, so the guard misses exactly the spellings the resolver accepts.

## Solution

The guard now computes the host's resolution set the way the resolver does and asserts
it is exactly loopback (`ansible/roles/services/navidrome/tasks/plugins.yml:115-129`):

```yaml
- name: Assert the analytics host resolves only to loopback inside the container
  ansible.builtin.assert:
    that:
      - navidrome_analytics_host_addrs == ['127.0.0.1']
    ...
  vars:
    navidrome_analytics_host_addrs: >-
      {{ navidrome_container_hosts.stdout_lines
         | map('regex_replace', '#.*$', '')
         | select('search', '(?i)\s' ~ (navidrome_musixmatch_analytics_host | regex_escape) ~ '\.?(\s|$)')
         | map('split') | map('first') | list }}
```

Step by step: strip comments, keep every line whose later fields name the host
(case-insensitive, optional trailing dot, name `regex_escape`d so its dots are
literal), take each kept line's address, and require the list to be exactly
`['127.0.0.1']`. A second line, an alias line or an IPv6 line adds an element. A
missing sinkhole leaves the list empty. Both fail.

The fence has a second dependency, and a companion assert pins it
(`plugins.yml:51-77`). The loaded manifest's `requiredHosts` must equal the reviewed
list EXACTLY, and its permission keys must equal `[cache, http, library]`. The exact
host list is what rules out the `*` and IP/CIDR entries that would let the netguard
pass a loopback dial. The key list matters because `websocket` carries its own host
list, which the http clause would not see. Both are read from Navidrome's own plugin
table (what it loaded), not from the file on disk.

Evidence, graded:

- **Offline-falsified against the real tasks file.** A matrix of 18 hosts-file fixtures
  and 10 plugin-row states ran through the actual `ansible/roles/services/navidrome/tasks/plugins.yml`. A fake `docker`
  script on `PATH` returned canned sqlite rows and hosts files. Every bad state above
  went red at its own assert, with that assert's `fail_msg`. The legitimate variants
  stayed green: tabs, CRLF line endings, an inline comment, leading whitespace, and
  names that merely contain the host as a substring. Harness tip: local runs need
  `-e 'ansible_python_interpreter={{ ansible_playbook_python }}'`, because Homebrew's
  python 3.15 crashes modules.
- **Live second apply:** green, `changed=0`, on the converged container.
- **Argued only:** that the netguard actually refuses the loopback dial. This comes
  from reading the 0.64.2 source and has not been seen refusing live. The compose
  comment and README "Plugins" say so, and a follow-up issue will record a live red.
  Until then, the fence's final link rests on a code reading.

## Why This Works

A hosts-file sinkhole is only as strong as the full answer the resolver gives for
that name. The resolver gathers addresses from every line, and the dialer falls
through refusals, so the safe state is "the complete set is {loopback}". The fixed
guard compares exactly that set, using matching rules at least as wide as the
resolver's, so the guard cannot see a smaller file than the resolver does.

The manifest assert covers the other half. The sinkhole works only because the
netguard refuses loopback for named hosts. That refusal has documented bypasses
(`*`, literal IP/CIDR). Pinning the exact host list and permission keys shuts those
bypasses, so a plugin version bump cannot quietly reopen the fence.

## Prevention

- **Assert the outcome the resolver produces, not the line you wrote.** For any
  `/etc/hosts`, `extra_hosts` or DNS sinkhole, compute the name's complete resolution
  set under the resolver's own matching rules (all alias columns, case, trailing dot,
  comments) and assert equality with the intended set. A presence check passes as soon
  as the intended line exists, which is the one state you already know is true.
- **When a fence depends on a second component's rule, pin that rule's bypasses too.**
  Write down the conditions under which the other component would let the traffic
  through (here: `*`, IP/CIDR, a host list in another permission) and assert each one
  is absent, from what the component actually loaded.
- **Falsify each bad state separately.** One fixture per escape route, each confirmed
  red at the guard's own `fail_msg`. Also include the legitimate formatting variants,
  so the stricter matcher does not fail healthy files.
- **Say which link is argued.** If part of the chain is a source reading, not a live
  observation, record it next to the code and track the live test in an issue.
  Do not let a green recap stand in for it.

## Related

- [guard-accept-condition-is-a-negative-fails-open](guard-accept-condition-is-a-negative-fails-open.md) — the parent thesis: accepting on "the thing I looked for is present" fails open on every shape not enumerated; the exact-set assert is its affirmative-allow fix.
- [guard-read-surface-must-match-loader-write-surface](../conventions/guard-read-surface-must-match-loader-write-surface.md) — same mechanism: the guard read one line, the consumer (Go's hosts lookup) reads every line.
- [prove-a-guard-whose-failing-state-cannot-exist-yet](../conventions/prove-a-guard-whose-failing-state-cannot-exist-yet.md) — the evidence grading used above (the netguard refusal is still "argued").
- [a-falsification-run-must-actually-reach-the-guard](../conventions/a-falsification-run-must-actually-reach-the-guard.md) — the fixture matrix drives the real tasks file to each assert.

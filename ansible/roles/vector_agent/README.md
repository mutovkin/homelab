# vector_agent

A **native (systemd) Vector log shipper** for the three hosts that have no
observability compose stack of their own: `eq12`, `n5pro` and `n5pro_docker`.

They write to the same VictoriaLogs instance the eq12_docker container writes to,
with the same record schema, and they label every record with the host's **Ansible
inventory name**.

```bash
cd ansible
ansible-playbook playbooks/log-agents.yml --check --diff --limit n5pro
ansible-playbook playbooks/log-agents.yml --limit n5pro
# or: task deploy:logagents:check -- --limit n5pro
```

## The four hostname labels, and the one that does not exist

`VECTOR_HOSTNAME` is `{{ inventory_hostname }}`, so the labels in VictoriaLogs are
exactly:

| Label | Host | Shipper |
| ----- | ---- | ------- |
| `eq12` | Proxmox hypervisor, 192.168.25.5 | this role |
| `n5pro` | Proxmox hypervisor, 192.168.30.5 | this role |
| `n5pro_docker` | CT 201, 192.168.30.15 | this role |
| `eq12_docker` | CT 101, 192.168.25.15 | the container, `roles/services/observability` |

**`pve` is not a hostname label.** It is eq12's Proxmox *node* name and its OS
hostname, and it appears in records only as the `syslog_hostname` field. #134's
issue text says "pve"; an alert or query written against it matches nothing.

## Shape, and why this shape

- **An agent per host, writing to VictoriaLogs over the LAN.** The alternative —
  rsyslog `omfwd` into a Vector `syslog` source on eq12_docker — needs a new
  listening port, needs eq12_docker's `vector.yaml` rewritten, and would label
  records with the OS hostname (`pve`, `deb-docker`), which is exactly the second
  hand-maintained mapping #143 existed to delete.
- **No new listening port anywhere**, on any host. This change adds *writers* to
  `:9428`; it does not widen it.
- **One shape for all three**, including n5pro_docker where a container was
  possible. A native Vector there reads `/var/run/docker.sock` directly and still
  collects container logs, so a container would buy nothing and cost a compose
  role, an `.env`, and a subnet out of the fleet map. One deployment shape means
  one code path and one verification path.
- **Nothing in `roles/services/observability`'s compose stack is touched.** Its
  `compose.yaml`, `templates/env.j2` and `files/data/vector/vector.yaml` are
  untouched by #134.

## What it deploys

| Path | Mode | Contents |
| ---- | ---- | -------- |
| `/var/cache/vector/vector_<ver>-1_amd64.deb` | 0644 | the pinned, checksummed release artefact |
| `/etc/vector/vector.yaml` | 0644 | from `templates/vector.yaml.j2` |
| `/etc/vector/vector.env` | **0600 root:root** | the systemd EnvironmentFile |
| `/etc/systemd/system/vector.service.d/10-homelab.conf` | 0644 | the override drop-in |

plus, via `include_role: rsyslog_structured`, `/etc/rsyslog.d/40-vector-structured.conf`
and `/etc/logrotate.d/vector-structured` — the RFC5424 side-stream Vector's only
host-log source tails.

## The four systemd traps

The unit the `.deb` ships is not safe to run unmodified. Each override in
`templates/vector-systemd-dropin.conf.j2` fixes a specific one; the role asserts
afterwards that the overrides actually took, because a drop-in that silently does
nothing is the default failure mode here.

1. **`ExecStartPre=/usr/bin/vector validate` runs sink healthchecks.** The sink is
   VictoriaLogs *on another host*. If eq12_docker is down or merely slower to boot,
   every hypervisor's `ExecStartPre` fails — and with trap 3, systemd then gives up
   permanently. Fixed by clearing `ExecStartPre=` and re-adding it with
   `--skip-healthchecks --config /etc/vector/vector.yaml`. Config errors are still
   caught; sink reachability is no longer a start precondition.
2. **`/etc/default/vector` is a dpkg conffile.** (Read off the package, not
   assumed: its `conffiles` list is `/etc/default/vector` plus
   `/etc/vector/examples/*` — `/etc/vector/vector.yaml` is **not** on it, so
   templating that file is safe.) Writing it makes every future
   `apt install ./vector_<ver>.deb` a modified-conffile situation. Our environment
   goes in `/etc/vector/vector.env`, which dpkg does not own, pointed at from the
   drop-in. Drop-in `EnvironmentFile=` entries are additive and later wins, so the
   base unit's `EnvironmentFile=-/etc/default/vector` staying put is harmless.
3. **`StartLimitInterval=10` / `StartLimitBurst=5`, in `[Service]`.** Five failed
   starts in ten seconds and systemd latches `failed` forever. The drop-in sets
   `[Unit] StartLimitIntervalSec=0` — a shipper that retries every ten seconds
   forever is strictly better than one nobody hears from again; the per-host
   recency alert is what makes a genuinely broken config visible. Note the
   `[Unit]`-overrides-`[Service]` asymmetry: the legacy spelling in the base unit is
   why the role asserts against the *effective merged unit* rather than trusting
   the file. And the property to assert on is **`StartLimitIntervalUSec`**, not
   `StartLimitIntervalSec` — the latter is the unit-file directive and
   `systemctl show -p StartLimitIntervalSec` prints nothing at all, so an assert
   written against that name would be inert. Measured on the fleet against
   `modprobe@.service`, which ships `StartLimitIntervalSec=0` and reports
   `StartLimitIntervalUSec=0`.
4. **`/etc/vector/vector.env` is a systemd EnvironmentFile, not a compose dotenv.**
   The parsers differ. Compose's dotenv interpolates `$` in unquoted values, which
   is why every `.env` in this repo is single-quoted (#117) — systemd does neither
   the interpolation nor the single-quote stripping. Values here are **double**
   quoted, which systemd strips, taking the contents literally including `$`. The
   role does not take that on trust: it reads `VL_AUTH_PASSWORD` back out of the
   running process's `/proc/<pid>/environ`, hashes it on the host, and asserts the
   digest matches the vault value's. Neither value is ever printed.

### What the VictoriaLogs credential may contain

One hard constraint, asserted before anything is written:
**`vault_vl_auth_username` and `vault_vl_auth_password` may not contain a double
quote, CR, LF or backslash.** Those are the characters systemd's own env-file
parser transforms — values are double-quoted, so a `"` truncates or swallows the
rest of the line, a newline reads as the start of a new assignment, and a
backslash is consumed when it escapes a quote or another backslash (measured
below). The result would be a credential Vector holds that is not the credential
in the vault, with a silent 401 as the only symptom.

Everything else is allowed, **including `$` and backticks**, and that is a
deliberate property rather than an omission. systemd does not interpolate inside
double quotes, so those characters are literal. The role only gets to say that
because nothing but systemd ever parses the file:

- the pre-flight validate runs via `systemd-run --property=EnvironmentFile=`, not
  a shell (see trap 4 and the note at the top of `templates/vector.env.j2`);
- the byte-for-byte digest assert is the universal backstop — whatever a future
  parser or a rotated value does to the credential, that assert fails the deploy
  loudly rather than letting a mangled value 401 in silence.

If you ever add a second reader of `/etc/vector/vector.env`, it must use systemd's
parser. Sourcing it from bash expands `$VAR` and **executes backticks as root** —
measured, not theorised.

#### What about backslash?

Measured too, because "systemd honours C-style escapes" is a plausible-sounding
claim that turns out to be only half right. One value through systemd's own
parser, bytes read back with `od -c`:

```
on disk    P_BS="tab\there-nl\nend-lit\qX-dq\"Y-bs\\Z"
systemd -> tab\there-nl\nend-lit\qX-dq"Y-bs\Z
```

`\t`, `\n` and `\q` come through **completely untouched** — systemd does *not* do
C-escape expansion on them, which refutes the "a rotated password containing `\n`
could be silently turned into a newline" concern. But `\"` collapsed to `"` and
`\\` collapsed to `\`: systemd *does* consume a backslash used to escape a quote
or another backslash.

A backslash is therefore mangle-able, so it is rejected alongside `"`, CR and LF.
That is not hardening against a hypothetical — it is the parser we actually use,
demonstrably transforming the value.

#### The thing that actually guarantees the credential arrived intact

Not the character class. **The `/proc/<pid>/environ` digest assert.** The character
rules only cover the mangling someone thought to look for; the digest compares what
the running Vector process holds against the vault value byte for byte, so it
catches mangling from *any* cause — a parser change in a future systemd, a
templating bug, a character nobody considered. If you are ever tempted to relax or
extend the character class, that digest assert is the control that must not be
touched.

## Verification the role performs on every run

In order, and none of it is optional:

1. Both credentials and both endpoints defined, non-empty, and free of `"`/newline
   — asserted **by variable name**, under `--check` too.
2. The rsyslog structured stream is live *right now* — a `logger` marker must reach
   `/var/log/structured.log` within 10 s (`roles/rsyslog_structured`).
3. `vector validate --skip-healthchecks` **before** a working shipper is restarted
   onto the new config — run through `systemd-run` as `User=vector` with
   `EnvironmentFile=`, so it is a faithful rehearsal of `ExecStartPre` rather than
   an approximation: same user as the unit, same parser for the env file, no shell
   anywhere in the path.
4. `systemctl is-active vector` → `active`, after a 15 s settle (Vector exits about
   a second after a bad config, so an immediate check proves nothing).
5. The drop-in is in force in the *effective merged unit*.
6. `VECTOR_HOSTNAME` in the running process equals `inventory_hostname`.
7. The credential digest match described above.
8. **The bar: an end-to-end ingest proof.** A unique marker is written with
   `logger`, and the role polls VictoriaLogs *from this host* until a record comes
   back matching `hostname:<inventory_hostname> source:host "<marker>"`. That single
   assertion covers rsyslog routing, structured.log being written, Vector reading
   it, the sink authenticating, VictoriaLogs storing it, and the hostname label
   being correct.

9. **Where `vector_agent_docker_logs` is true, two more** — because every check
   above passes with the `docker_logs` source completely dead. `getent` passes
   (the group exists either way), the `user` task passes (it edits `/etc/group`,
   which says nothing about the running process), `vector validate` passes (it
   does not touch the socket under `--skip-healthchecks`), `ActiveState=active`
   passes (Vector runs happily with one broken source), and step 8 passes because
   it filters `source:host` and its marker arrives via `logger`. So:
   a. the **running process's** effective supplementary groups, read from
      `/proc/<pid>/status`, must contain the docker gid — `/etc/group` membership
      is a different fact, since systemd resolves groups at exec time;
   b. VictoriaLogs must hold `source:docker` records for this host in the last 15
      minutes. No marker here (that would mean starting a throwaway container on a
      live host), so it asserts on organic volume — CT 201's three containers are
      chatty enough. If that ever stops being true, widen the window and record the
      measurement; do not delete the check.

"The process is running" is exactly the evidence that stayed green for the ~30 days
the log pipeline was dead
([#73](../../../docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md)).
That is why steps 8 and 9 exist and why neither is gated on anything having
changed.

## Bumping the pinned version

Watchtower does **not** cover this. It watches container image references; this is
a `.deb` installed by dpkg, so no notification will ever arrive about it. Bumping
is a manual, deliberate act:

1. Read <https://github.com/vectordotdev/vector/releases> for breaking changes.
   Vector has shipped two in one release before, and only one of them was loud
   (#73) — read the whole upgrade guide, not the entry matching your error.
2. Take the sha256 from that release's own `vector-<ver>-SHA256SUMS`.
3. Update **both** `vector_agent_version` and `vector_agent_deb_sha256` in
   `defaults/main.yml`. They are pinned in one place; there is no second copy.
4. `--check --diff` first, then apply one host at a time. The role's own ingest
   proof is the gate — it fails the play if the new version ships nothing.

There is deliberately **no stat-exists gate** on the download. `get_url` hashes the
file already on disk, skips the fetch on a match and re-downloads on a mismatch; a
`when: not stat.exists` guard would make a corrupt cached `.deb` trusted forever
(CLAUDE.md).

The role prunes the cache itself: after the download it removes every
`vector_*_amd64.deb` in `/var/cache/vector` **except the pinned one**, so a bump
does not leave another ~36 MB artefact behind forever. Only superseded files go —
keeping the current one is what makes a re-run cheap and is what `apt` installs
from.

### Version skew with the container

eq12_docker's Vector container runs `timberio/vector:latest-distroless-static` —
a floating tag, updated by watchtower. This role pins `0.57.0`. They are equal
*today* (both measured 0.57.0 on 2026-08-19) and that is a coincidence of timing,
not a mechanism: the container floats and this does not, so they will drift.
Tracked as its own follow-up issue.

## Docker log collection

`vector_agent_docker_logs` is `false` by default and `true` only in
`host_vars/n5pro_docker/vars.yml`. When true the role adds the `vector` user to the
`docker` group after installing the package (the package's preinst creates the
user).

**That is root-equivalent access on that host** — anyone who can reach the socket
can start a privileged container. It is the same posture the vector *container* has
on eq12_docker via its `/var/run/docker.sock` bind-mount, and the same telegraf
already holds there: a new holder of an existing grant, not a new kind of grant.
The role asserts the `docker` group exists rather than letting the `user` module
invent one with the wrong gid, which would be a silent no-op — and then, after the
restart, asserts that the **running process** actually holds that gid and that
container records are actually landing in VictoriaLogs (verification steps 9a/9b
above). Declaring the capability and proving it are different things, and only the
second one survives contact with a socket permission change.

On a host where `vector_agent_docker_logs` is false the `docker_logs` source is not
merely unused — it is absent from the rendered config. A `docker_logs` source with
no socket makes Vector log a connection error on a loop forever while still
shipping host records: a permanently noisy, permanently green shipper.

## What running an agent on a host buys

- **Kernel and OOM-kill events**, for the first time, on all three. The `*.*`
  selector in the structured drop-in carries the `kern` facility, and all three
  hosts have a real kernel log (measured — including the *privileged* CT 201, whose
  `imklog` is loaded and whose `kern.log` carries live `veth*`/`docker0` lines).
  `/var/log/kern.log` is deliberately **not** added as a second source: it would
  double-ingest every kernel line that the `*.*` selector already carries.
- **unattended-upgrades results and failures on the hypervisors** land where an
  alert looks. That is the gap #99 opened and #134 closes.
- **`auth.log` on the hypervisors** — the SSH-facing surface of the whole fleet —
  becomes queryable and alertable.
- **A single dead shipper is detectable**, via the per-host recency rule
  `obs-host-log-ingest-stalled-per-host`
  (`roles/services/observability/files/data/grafana/provisioning/alerting/per-host-ingest-health.yaml`).

## Deliberately not done here

- No listening port is added, and `:9428` is not narrowed. It is already open to
  the flat LAN; this adds writers to it. Narrowing needs its own
  from-a-blocked-source verification and its own establishment of NPM/operator
  needs — a follow-up issue.
- No TLS on the ingest path. Basic-auth credentials cross the LAN in cleartext,
  same as every other internal hop in this homelab. Stated, not quietly shipped —
  follow-up issue.
- VictoriaLogs' `--httpAuth` is **one credential pair for the whole instance**, so
  a shipping host necessarily holds read+write credentials. That is why the vars
  moved to `group_vars/all/vault.yml`.
- No telegraf/metrics on these hosts. #134 is logs.
- The boot-window gap is inherited, not closed: these agents tail the same
  `structured.log`, so units that log before `rsyslog.service` starts reach neither
  file (limitation 2 in the observability README).

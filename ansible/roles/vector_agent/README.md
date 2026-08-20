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

## Verification the role performs on every run

In order, and none of it is optional:

1. Both credentials and both endpoints defined, non-empty, and free of `"`/newline
   — asserted **by variable name**, under `--check` too.
2. The rsyslog structured stream is live *right now* — a `logger` marker must reach
   `/var/log/structured.log` within 10 s (`roles/rsyslog_structured`).
3. `vector validate --skip-healthchecks`, sourcing the real env file, **before** a
   working shipper is restarted onto the new config.
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

"The process is running" is exactly the evidence that stayed green for the ~30 days
the log pipeline was dead
([#73](../../../docs/solutions/integration-issues/vector-057-silent-log-pipeline-failure.md)).
That is why step 8 exists and why it is not gated on anything having changed.

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
invent one with the wrong gid, which would be a silent no-op.

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

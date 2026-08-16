---
title: "Mounted is not wired: postgres configs were never deployed, and pg_hba.conf was never read"
date: 2026-08-15
category: integration-issues
module: services/postgresql
problem_type: integration_issue
component: tooling
symptoms:
  - "Repo-tracked bind-mounted configs (postgresql.conf, pg_hba.conf, init scripts, pgadmin servers.json) are never deployed by the role — editing them in git changes nothing on the live host"
  - "SHOW hba_file returns the $PGDATA image-initdb default, proving the mounted pg_hba.conf was never read even though the mount is present and correct"
  - "Live pg_hba.conf has drifted to hand-edited entries — one subnet that misses the clients' network, one CIDR the server would reject outright — yet auth keeps working"
  - "Restarting pgadmin after a servers.json change does nothing — the image imports it only on first launch or with PGADMIN_REPLACE_SERVERS_ON_STARTUP=True"
  - "On a fresh host every config task reports changed, so a naive changed-to-restart pairing bounces postgres seconds after compose up and races the entrypoint initdb"
root_cause: incomplete_setup
resolution_type: code_fix
severity: high
related_components:
  - postgresql
  - pgadmin
  - joplin
  - docker-compose
  - services/_deploy
tags:
  - postgresql
  - pgadmin
  - ansible
  - bind-mount
  - config-drift
  - silent-failure
  - initdb-race
  - restart-gating
---

# Mounted is not wired: postgres configs were never deployed, and pg_hba.conf was never read

## Problem

`ansible/roles/services/postgresql` tracked four config files — `postgresql.conf`,
`pg_hba.conf`, `init-scripts/01-init-databases.sql`, `pgadmin/servers.json` — and deployed
**none** of them. The role created directories and called the shared `services/_deploy`
pipeline; the compose file bind-mounts the files from `/data/postgresql/...`, not from the
deploy dir. The repo copies were decorative and the live files were whatever was last
hand-copied — #73's gotcha, repeated verbatim in a second role.

Fixing that surfaced a second, worse layer. Even the file that *was* mounted at the right
path was not being read: `pg_hba.conf` had been inert since the stack was first deployed,
because nothing told the server to look at it.

## Symptoms

- Live `pg_hba.conf` on eq12_docker had drifted to two hand-edited entries: `172.22.0.0/16`,
  which does not cover `postgres_network`'s actual subnet (`172.21.0.0/24`), and
  `172.22.0.0/12`, which is not a valid pg_hba CIDR at all — host bits are set below the
  mask, so Postgres would have rejected the file outright had it ever read it. Postgres was
  nonetheless accepting connections from that network, which is the tell.
- App-level introspection on the running server:

  ```
  SHOW config_file;  -> /etc/postgresql/postgresql.conf            # the mount, honoured
  SHOW hba_file;     -> /var/lib/postgresql/18/docker/pg_hba.conf  # $PGDATA, the initdb default
  ```

  Two bind mounts on the same container: one live, its sibling ignored.
- Effective auth was therefore the image default (`local all trust`, loopback trust,
  `host all all all scram-sha-256`) — not the repo's file, not the hand-edited file.
- `docker inspect` showed both mounts present and correct. Every container-level check
  passed.
- `/data/postgresql/pgadmin` was root-owned on disk (hand-fixed to 5050 at some point);
  pgadmin runs as uid 5050 and crash-loops on a fresh host where Docker auto-creates it.

## What Didn't Work

- **Trusting `docker inspect`.** The mount was there, the path was right, the file
  contents were on disk inside the container. None of that means the process opened it.
  Only `SHOW hba_file;` distinguishes "mounted" from "read".
- **Reasoning by analogy from `config_file`.** `postgresql.conf` works because
  `ansible/roles/services/postgresql/files/compose.yaml` passes
  `command: postgres -c config_file=/etc/postgresql/postgresql.conf`.
  There is no equivalent flag for `hba_file`, and Postgres defaults it to
  `$PGDATA/pg_hba.conf` — so the identical-looking mount one line below silently did
  nothing.
- **Pairing `servers.json` with a pgAdmin restart.** The obvious symmetry with the Vector
  fix ("deploy the file, restart the service") is inert here: read against the running
  image, `dpage/pgadmin4`'s entrypoint calls `load_server_json_file` only in the
  *not-yet-initialised* branch (empty `/var/lib/pgadmin`) or under
  `PGADMIN_REPLACE_SERVERS_ON_STARTUP=True`, which this stack does not set. The live volume already had `pgadmin4.db`. A restart would have been
  theatre.
- **The naive restart pairing itself.** "Config task reports changed → restart the
  container" is a fresh-host landmine. On a never-deployed host every config task reports
  changed, `compose up` creates postgres *from those very files*, and the restart fires
  seconds later — racing the entrypoint's `initdb`. `PG_VERSION` is written early, so an
  interrupted first boot leaves a data dir that *looks* initialised: the entrypoint skips
  `/docker-entrypoint-initdb.d` forever, the databases and users the init script would have
  created never exist, and the play reports green.
- **Expecting `--check --diff` to prove any of this.** A dry run cannot show that the new
  `hba_file` setting takes effect, and the restart and readiness tasks are skipped under
  `--check` by construction. The same trap had already been recorded on this repo: a
  routine run where all five guarded backup tasks skipped was green and proved nothing
  (#72), and check mode evaluates the *old* payload for anything the sync has not shipped
  yet (session history).

## Solution

**1. Pin `hba_file` so the mount is actually read** — in
`ansible/roles/services/postgresql/files/config/postgresql.conf`:

```conf
hba_file = '/etc/postgresql/pg_hba.conf'
```

`hba_file` is a postmaster (startup-only) parameter — a reload will not pick up the
cutover, which is why the restart below is the conservative blanket action.

**2. Deploy the configs to the mount paths**, in
`ansible/roles/services/postgresql/tasks/main.yml`:

```yaml
- name: Deploy PostgreSQL server configs
  ansible.builtin.copy:
    src: "config/{{ item }}"
    dest: "{{ data_mount }}/postgresql/config/{{ item }}"
    mode: "0644"
  loop:
    - postgresql.conf
    - pg_hba.conf
  register: postgres_configs
```

and exclude `config/` from the `_deploy` rsync payload so no decorative second copy exists:

```yaml
- name: Deploy PostgreSQL via the shared service pipeline
  ansible.builtin.include_role:
    name: services/_deploy
  vars:
    svc: postgresql
    svc_rsync_extra:
      - "--exclude=config/"
```

**3. Probe before deploying, so the restart can never hit a container this run created:**

```yaml
- name: Check whether the postgres container pre-exists
  community.docker.docker_container_info:
    name: postgres
  register: postgres_container_info
```

registered **above** the `_deploy` include, and folded into a single decision fact:

```yaml
- name: Determine containers to restart for changed config
  ansible.builtin.set_fact:
    postgres_restart_targets: >-
      {{ ['postgres'] if (postgres_configs.results | selectattr('changed') | list | length > 0
          and postgres_container_info.exists) else [] }}
```

Deliberately **no `default()` filters**: if a register above is renamed, the play must fail
loudly rather than silently skip the restart — silent do-nothing is the exact failure mode
this issue is about.

**4. Preview in check mode**, because the restart task itself must be skipped there
(`docker_container` fails against a container that does not exist rather than previewing
anything), and a dry run that hides a client-visible outage is worse than no dry run:

```yaml
- name: Preview config-change restart (check mode)
  ansible.builtin.debug:
    msg: "Config change WILL RESTART on real apply: {{ postgres_restart_targets }} (client-visible outage for DB clients)"
  when:
    - ansible_check_mode
    - postgres_restart_targets | length > 0
```

**5. Prove the server came back.** `docker_container` with `restart: true` returns when the
restart is *issued*, not when the service is serving — and now that `hba_file` is really
read, a malformed `pg_hba.conf` crash-loops postgres while the play reports green:

```yaml
- name: Wait for postgres to accept connections after restart
  community.docker.docker_container_exec:
    container: postgres
    command: pg_isready -U postgres
  register: postgres_ready
  until: postgres_ready.rc == 0
  retries: 12
  delay: 5
  changed_when: false
  when:
    - not ansible_check_mode
    - "'postgres' in postgres_restart_targets"
```

**6. Secondary fixes in the same change.** The init script became
`ansible/roles/services/postgresql/templates/01-init-databases.sql.j2`, rendering
`vault_joplin_postgres_password` and
deployed owner `999` / group `999` / mode `0600` with `diff: false` — a `--diff` would
otherwise print the rendered credential (the repo has a prior incident where a `--diff`
leaked a Gmail app password). The tracked plaintext password was removed from git.
`/data/postgresql/pgadmin` is now created owner/group `5050` by the role. `servers.json` is
deployed but deliberately paired with **no** restart, with the reason written in the task
comment.

## Why This Works

`config_file` and `hba_file` fail at different layers, exactly like Vector's two breaking
changes. `config_file` is passed on the command line, so a wrong path is a startup failure —
loud. `hba_file` has a working default inside `$PGDATA`, so a missing pin is not an error at
all: the server starts, serves traffic, and uses a config nobody in the repo wrote. There is
no log line, no failed healthcheck, and no `docker inspect` field that differs. The only
signal is asking the running process which file it opened.

The restart guard works because the probe runs *before* `_deploy`. `postgres_container_info.exists`
is a fact about the world as it was at the start of the run, so `postgres_restart_targets`
can never name a container that this run just created — a first deploy deliberately
restarts nothing, and the entrypoint's `initdb` is left alone to finish and run
`/docker-entrypoint-initdb.d`. The readiness gate closes the other end: the change that made
`pg_hba.conf` load-bearing is the same change that made a typo in it fatal, so the play now
refuses to finish until `pg_isready` succeeds against the restarted container.

Live outcome on eq12_docker: `SHOW hba_file;` returns `/etc/postgresql/pg_hba.conf`;
postgres was **restarted, not recreated**; a second apply reported `changed=0`; the
readiness gate ran and passed; and a comment-only edit to a config file was proven to reach
the live container and trigger the restart.

## Prevention

- **Ask the process which file it read, not Docker which file it mounted.** For any
  bind-mounted config, the check is app-level introspection — `SHOW hba_file;` /
  `SHOW config_file;` for Postgres, `nginx -T`, `SHOW VARIABLES` — never `docker inspect`
  mount presence. A mount is a filesystem fact; reading it is an application decision.
- **When one mounted config works, do not assume its siblings do.** `postgresql.conf` was
  honoured only because compose passes `-c config_file=...`. Enumerate what makes *each*
  mounted file reachable, per file.
- **Compare live against repo before adopting a hand-managed file.** Hash both copies first
  (the Vector adoption did exactly this and found them byte-identical); here they had
  drifted, and knowing that up front is what made "the repo copy is canonical" a decision
  rather than an accident (session history).
- **Probe before you deploy, restart only what pre-existed.** Register a
  `community.docker.docker_container_info` above the deploy and gate the restart on
  `.exists`. Restarting a container the same run created races its entrypoint's
  first-boot initialisation — and for Postgres that failure is permanent and silent.
- **A restart is not a return to service — gate on readiness.** Follow every explicit
  restart with an `until`/`retries` probe (`pg_isready -U postgres`, an HTTP `/health`),
  `changed_when: false`. Modules return when the command is issued, not when the service
  is serving.
- **Preview destructive actions in check mode.** If a task must be skipped under
  `--check`, add an `ansible.builtin.debug` that states what the real run would do, or the
  documented dry-run-before-apply workflow hides a client-visible outage.
- **A guard that never fired is a guard that was never tested.** The restart, the
  pre-existence probe and the readiness gate are all skipped in check mode, so the only
  proof they work is a live apply that actually exercises them — plan the change so one
  apply does (session history, #72).
- **No `default()` in a restart decision.** `default([])` on a register turns a renamed
  variable into a silently skipped restart. Let it fail loudly instead.
- **Never pair a restart with a config that is only read at first init.** Verify against
  the image entrypoint (`servers.json` is imported only on a fresh `/var/lib/pgadmin`).
  Deploy the file anyway — it is what configures a fresh host — and record in the task
  comment why no restart accompanies it.
- **`diff: false` on any template rendering a secret**, plus restrictive `owner`/`mode`.
  A `--diff` run is the standard workflow here, and it prints rendered content.

## Related Issues

- #78 — this fix
- [[vector-057-silent-log-pipeline-failure]] — #73, the first instance of this class. Its
  rule ("if a compose file bind-mounts a config path, Ansible must own that path") is
  necessary but not sufficient: deploying is not wiring.
- [[searxng-use-default-settings-and-braveapi]] — third instance, config deployed to the
  deploy dir instead of the bind-mount path. It pairs changes with `recreate: always`;
  a stateful, shared service like postgres takes a targeted restart plus a readiness gate
  instead.
- [[collocating-compose-stacks-into-ansible-roles]] — #85 removed the mirror-pair drift
  class repo-side and supplies the `services/_deploy` pipeline this fix wraps; it named
  postgresql's undeployed configs as the surviving instance.
- [[unattended-upgrades-silently-inert-fleet-wide]] — the same silent-green shape in another
  subsystem: an artifact that exists, looks correct, and is consumed by nobody.
- [[compose-up-recreates-watchtower-created-containers]] — before blaming a config change
  for a bounce, check container lineage; postgres and pgadmin4 are its worked example.
- [[ansible-change-loop-pitfalls]] — check-mode honesty and existence gates, the conventions
  the restart block navigates.

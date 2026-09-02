# Immich

Self-hosted photo and video management. The stack is **latent**: the role is complete
and `task compose:validate` parses it, but no host's `services:` list names it, so
nothing deploys it yet (#91). The only inventory that mentions immich at all is
`ansible/inventory/host_vars/n5pro_docker/vars.yml` — as a commented-out line (`:44`)
inside a whole alternate `services:` block, and as its pinned subnet (`:70`). To enable
it, add `- immich` to that host's **active** `services:` list (do not uncomment the
block — it would also enable postgresql, frigate and nextcloud), then
`task deploy:service -- --tags immich`.

## Services

All four containers live on the role's own `immich_network`. Only `immich-server`
publishes a port; the other three are reachable on that network only.

| Service                 | Image                                                                   | Port            | Purpose                                            |
| ----------------------- | ----------------------------------------------------------------------- | --------------- | -------------------------------------------------- |
| immich-server           | `ghcr.io/immich-app/immich-server:release`                              | 2283            | Main API + web UI                                  |
| immich-machine-learning | `ghcr.io/immich-app/immich-machine-learning:release-rocm`               | 3003 (internal) | Face/object/CLIP inference on the GPU (#244)       |
| immich-postgres         | `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0` (digest-pinned) | 5432 (internal) | Dedicated VectorChord-enabled PostgreSQL 14 (#91) |
| immich-redis            | `redis:7-alpine`                                                        | 6379 (internal) | Cache and job queue                                |

`immich-server` and `immich-machine-learning` are pinned by tag *family* (`release` /
`release-rocm`) — Immich requires both to run the same release, so they move together.

## Database — its own PostgreSQL, not the shared one

Immich does **not** use the shared `postgresql` role's server (#91) — which does not even
run on immich's own host: `postgresql` is deployed only on eq12_docker. It runs the
`immich-postgres` sidecar because:

- Immich's supported matrix is PostgreSQL 14–17 **with VectorChord**; the shared cluster
  is vanilla `postgres:18` with no vector extension and no `immich` database in its init
  script — pointing at it would produce a server that cannot finish startup.
- Adding an extension + `shared_preload_libraries` to the cluster that backs joplin (and
  nextcloud, when it is enabled) would make every other service inherit Immich's upgrade
  constraints.
- The stack deliberately does not attach to the shared external `postgres_network`, so a
  compromised photo service cannot reach the shared cluster at all.

Mechanics, all in `files/compose.yaml`:

- **Image is digest-pinned**
  (`@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23`) to the
  exact reference in Immich's own `docker/docker-compose.yml` for the release this stack
  tracks (verified 2026-08-18 against v3.1.0). Upstream ships DB and server as a matched
  pair — a mismatched vector extension is a failed startup, not a warning. **Bump it
  together with immich, from that file, never independently.**
- **Watchtower can report nothing about it.** Watchtower only checks the exact reference a
  container runs, and a digest cannot be re-pushed, so the `enable` label merely keeps it
  counted in `scanned=N`; `monitor-only` makes that doubly true (#83). The bump rides on
  the server's.
- Data lives in the **named volume** `immich-pgdata` (not a bind mount: `initdb` must own
  and create the path on a fresh host). `POSTGRES_INITDB_ARGS: --data-checksums`,
  `shm_size: 128mb`.
- The image's own `HEALTHCHECK` (which also verifies vector-index integrity) is
  re-enabled with `healthcheck: disable: false`, and `immich-server` waits on it via
  `depends_on: condition: service_healthy` — safe because a real binary exists in the image.
- `no-new-privileges:true` is safe here even though the entrypoint drops root → postgres:
  that image chain uses `gosu`, which is not setuid (it calls `setuid(2)` directly), and
  `no_new_privs` only blocks privilege *gain* through `execve` of setuid/fcaps binaries.

Credentials come from the vault via `templates/env.j2` (every value single-quoted, #117):

| `.env` var           | Source                                        |
| -------------------- | --------------------------------------------- |
| `IMMICH_DB_USERNAME` | `vault_immich_db_username` (default `immich`) |
| `IMMICH_DB_PASSWORD` | `vault_immich_db_password` (required)         |
| `IMMICH_DB_DATABASE` | `vault_immich_db_database` (default `immich`) |

The same three values feed `immich-postgres` (`POSTGRES_USER/PASSWORD/DB`) and
`immich-server` (`DB_USERNAME/DB_PASSWORD/DB_DATABASE_NAME`, with
`DB_HOSTNAME: immich-postgres`).

## Watchtower posture (#83)

| Container               | Labels                    | Why                                                                                         |
| ----------------------- | ------------------------- | ------------------------------------------------------------------------------------------- |
| immich-server           | `enable` + `monitor-only` | Stateful; every release runs one-way DB migrations. Reported, updated only by a deliberate deploy after a DB dump. |
| immich-machine-learning | `enable` + `monitor-only` | Stateless, but must stay in release lockstep with the server.                              |
| immich-postgres         | `enable` + `monitor-only` | Stateful and digest-pinned (see above).                                                     |
| immich-redis            | `enable` only             | Cache-only state, major-pinned `7`; recreate-from-image is the recovery path.               |

## GPU acceleration (AMD Radeon 890M)

- `immich-server` uses VAAPI for video transcoding and needs `/dev/dri` only.
- `immich-machine-learning` runs Immich's `-rocm` image (ROCm 7.2 + MIGraphX, Immich's
  only ROCm flavour) and needs `/dev/kfd` **as well as** `/dev/dri` — kfd is the compute
  driver, dri carries the render node it dispatches through. That container decodes no
  video, so its `/dev/dri` is not there for VAAPI; dropping it as "unused" breaks ROCm
  (#244). The kernel-side `/dev/kfd` path is independent of the CT's userspace ROCm 10
  (#240).
- `MACHINE_LEARNING_MODEL_TTL: 300` is Immich's own default, pinned explicitly so an
  upstream change cannot silently widen the ~5 minutes of elevated idle GPU draw a
  loaded model causes after each inference. It is **not** a mitigation of that draw —
  lower it (never `<= 0`, which disables unloading) if idle power is measured to matter.
  See [a-knob-set-to-its-default-is-not-a-mitigation](../../../../docs/solutions/conventions/a-knob-set-to-its-default-is-not-a-mitigation.md).
- Both devices are Docker device mappings; the LXC host bind-mounts them into CT 201
  via the `proxmox_guests` role (`gpu_sharing: true` in `host_vars/n5pro/vars.yml`).

Deploy day, before enabling immich on a host: the `-rocm` ML image is ~35 GiB unpacked
(check the CT rootfs with `df -h /`; `root_disk_size` in `host_vars/n5pro` is grow-only
live-reconciled), and MIGraphX compiles models on first inference — minutes of 100% GPU
is startup, not a hang.

## Storage and network

- Uploads: `UPLOAD_LOCATION` = `{{ data_mount }}/immich/upload` (`/data/immich/upload`),
  created by the shared `services/_deploy` pipeline from `svc_data_dirs` in
  `tasks/main.yml`, bind-mounted at `/usr/src/app/upload`.
- Named volumes: `immich-pgdata` (database), `immich-model-cache` (ML models, `/cache`),
  `immich-redis-data`.
- `immich_network` subnet comes from `docker_networks.immich` in the host's `host_vars`
  (`172.31.0.0/24` on n5pro_docker, the fleet map); the `env.j2` lookup is `mandatory()`
  and fails the template rather than falling back.

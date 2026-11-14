# services/npm: Nginx Proxy Manager

The reverse proxy and TLS terminator for every `<domain>` service. It runs as a compose
service on eq12_docker (CT 101). It replaced the proxmox-helper-scripts LXC **CT 104** (#331),
which is kept as a **stopped, non-autostarting cold standby and is never deleted**.

Plan and rationale: `docs/superpowers/plans/2026-11-14-npm-docker-migration.md`.
Incidents behind it: #329.

## What lives where

| Thing | Where | In the repo? |
| ----- | ----- | ------------ |
| Proxy hosts, admin login, DNS-provider (Cloudflare) token | NPM's SQLite DB, `{{ npm_data_dir }}/data/database.sqlite` | **No.** It's data, migrated once from CT 104 and backed up by the role |
| Generated nginx confs | `{{ npm_data_dir }}/data/nginx/` (NPM regenerates them from the DB) | No |
| Certificates and private keys | `{{ npm_data_dir }}/letsencrypt/` → `/etc/letsencrypt` | No |
| Docker subnet | `docker_networks.npm` (172.17.0.0/24; see the fleet map in `host_vars/n5pro_docker/vars.yml`) | Yes |
| Admin UI allowlist (:81) | `npm_firewall` in `host_vars/eq12_docker/vars.yml` | Yes |

The repo is public. **Never** put a hostname, upstream list or credential from NPM's UI in
this repo; `task lint:vault-leaks` enforces the vault-backed half of that (#333).

## Guards

- **No database, no start.** The role asserts `database.sqlite` exists before deploying,
  regardless of flags. A first start without it would create an empty schema and NPM's
  default admin account on the LAN.
- **One-shot seed** (`tasks/seed.yml`), armed by `-e npm_seed_from_ct104=true` and an
  absent destination DB. It places the DB **last**, so a failed run stays re-runnable. It
  compares the staged DB's proxy hosts and certificates with the source **by name**, and
  checks that every certificate a seeded conf references exists. Rehearse first:
  `task deploy:service -- --limit eq12_docker --tags npm -e npm_seed_from_ct104=true -e npm_seed_only=true -e npm_seed_dest=/data/npm-seedtest`
- **Pre-upgrade backup.** When the resolved image differs from the running one: stop,
  archive `{{ npm_data_dir }}`, verify that the archive's DB opens and has proxy hosts,
  then deploy. A failed backup restarts the old image and refuses the upgrade.

## Updates

The image is pinned to an exact version (`files/compose.yaml`). NPM runs one-way schema
migrations on start, and `_deploy` runs `pull: always`, so a floating tag would make every
deploy an unreviewed upgrade of the front door. Watchtower cannot report releases for an
immutable tag. Check <https://github.com/NginxProxyManager/nginx-proxy-manager/releases> by
hand, read the notes, bump the tag, deploy (the backup gate runs), and record it below.

From 2.16.0 the DNS credentials file is deleted after every certbot run, so a hand-run
`certbot renew --dry-run` fails for an unrelated reason. Test renewal through NPM itself.

## Bump log

| Date | From → To | Notes |
| ---- | --------- | ----- |

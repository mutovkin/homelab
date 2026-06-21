---
title: "SearXNG: stale engine errors, missing braveapi, and config that never deploys"
date: 2026-06-21
category: integration-issues
module: services/searxng
problem_type: integration_issue
component: tooling
symptoms:
  - "Cannot load engine X errors at startup for engines removed/renamed upstream"
  - "braveapi engine absent from /config and never queried (unresponsive_engines empty)"
  - "422 Unprocessable Entity returned by the Brave Search API"
  - "edits to the repo settings.yml never reach the running container"
root_cause: config_error
resolution_type: config_change
severity: low
related_components:
  - searxng
  - docker-compose
  - ansible-vault
tags:
  - searxng
  - ansible
  - docker-compose
  - use_default_settings
  - braveapi
  - vault
---

# SearXNG: stale engine errors, missing braveapi, and config that never deploys

## Problem

SearXNG (CT 101) pinned the full ~2831-line upstream `settings.yml` in the repo.
It drifted from the `searxng:latest` image, so on every startup 11 engines whose
upstream modules had been removed or renamed failed to load. Separately, the paid
Brave Search API engine (`braveapi`) never returned results, and Ansible config
edits weren't reaching the running container at all.

## Symptoms

- Startup logs: `ERROR:searx.engines: Cannot load engine "ask"/"stract"/"svgrepo"/"seekr"/...`
  then `loading engine X failed: set engine to inactive!` — 11 engines total.
- `braveapi` absent from `/config`, produced **no** load error, and was never
  queried — search JSON showed `unresponsive_engines: []` with no `braveapi` results.
- After activating `braveapi`: `422 Unprocessable Entity` from `api.search.brave.com`.
- Free `brave` engine: `SearxEngineTooManyRequestsException: Too many requests`.
- Edits to the repo's `settings.yml` never changed the running container.

## What Didn't Work

- **Removing individual stale engine entries from the pinned `settings.yml`** —
  whack-a-mole; the file drifts again on every image update.
- **Setting onion engines (`ahmia`, `torch`) `disabled: true`** — still triggers a
  load error at startup; engines must be dropped via `engines.remove`, not disabled.
- **Editing `containers/searxng/config/settings.yml` and re-deploying** — changes
  never reached the container due to a deploy-path mismatch (below).
- **Re-reading the `braveapi` key from the live host file to re-template it** —
  fragile; a mis-render dropped the key entirely, and it could not be recovered
  from the now-overwritten file. Secrets must come from vault.

## Solution

**1. Replace the pinned full config with a minimal `use_default_settings` override**
so the engine catalog always tracks the installed image:

```yaml
use_default_settings:
  engines:
    remove: [ahmia, torch, brave, brave.images, brave.videos, brave.news]
general: { instance_name: "MutoSearch" }
server: { secret_key: "ultrasecretkey", limiter: false }   # overwritten by ${SEARXNG_SECRET}
search: { autocomplete: "duckduckgo", favicon_resolver: "duckduckgo", formats: [html, json] }
```

**2. Fix the deploy-path mismatch.** The Ansible role synced config to
`/data/deploy/searxng/config`, but the container bind-mounts
`/data/searxng/config`. Template `settings.yml` directly to the real bind-mount
path and recreate the container on change (compose won't restart on a
bind-mounted file's content change alone):

```yaml
recreate: "{{ 'always' if searxng_settings_tpl.changed else 'auto' }}"
```

**3. Activate `braveapi` correctly** — two non-obvious traps:

- The image default ships `braveapi` with `inactive: true`. Under
  `use_default_settings`, a top-level engine entry is **merged by name** into the
  default (`settings_loader.py: update_dict`), so `inactive: true` survives unless
  you explicitly set `inactive: false`.
- The Brave Web Search API caps `count` at 20; any `results_per_page > 20` makes
  every call return 422. Set `results_per_page: 20`.

```yaml
engines:
  - name: braveapi
    engine: braveapi
    api_key: "{{ vault_searxng_brave_api_key }}"   # from ansible-vault
    results_per_page: 20
    inactive: false
```

**4. Source the Brave key from ansible-vault**, gated so it never lands in the
public repo (do **not** read it from the host file):

```yaml
{% if vault_searxng_brave_api_key is defined and vault_searxng_brave_api_key | length > 0 %}
    api_key: "{{ vault_searxng_brave_api_key }}"
{% endif %}
```

## Why This Works

- `use_default_settings` inherits the image's own engine catalog — zero drift on
  image updates; removed/renamed engines disappear automatically instead of
  failing to load.
- `engines.remove` drops engines *before* the load phase; `disabled: true` only
  deactivates an engine that still attempts to load, so it still errors.
- The `braveapi` default is `inactive: true`, and the by-name merge preserves any
  flag the user entry doesn't set — so `inactive` must be explicitly set `false`.
- Brave's API enforces a hard `count` cap of 20; exceeding it is a
  request-parameter error (422), not a transient failure.
- Templating to the actual bind-mount path with recreate-on-change ensures config
  edits both arrive in the container and take effect.

## Favicon resolver caveat — it took down search, then worked (#63, #65)

Setting `search.favicon_resolver` makes the result template call `favicon_url()`
for **every** result, which opens a SQLite favicon cache. The cache **path must
be writable by the searxng worker** or every search 500s
(`unable to open database file` → the Jinja result template raises).

The non-obvious trap: the `searxng/searxng` worker runs **as root (uid 0)**, but
the compose stack uses `cap_drop: ALL` **without `DAC_OVERRIDE`**, so root can
**not** bypass file permissions. The `/data/searxng/data` → `/var/cache/searxng`
bind mount is owned `977:977` mode 755, so the root worker can't write it. A
`favicons.toml` that pointed `db_url` there (#61) → 500 on every search (#63).
(Without any `favicons.toml` it "only" logs `missing favicon config` and 404s.)

**Working fix (#65): do NOT override `db_url`.** searxng's default favicon cache
is `/tmp/faviconcache.db`; `/tmp` is `1777`, writable by the root worker. Ship a
minimal `favicons.toml` (`cfg_schema = 1`, no `[favicons.cache]` override) +
`search.favicon_resolver: duckduckgo`. Favicons then render and the proxy serves
real icons (`/favicon_proxy?authority=…` → 200 `image/x-icon`). The cache is
ephemeral (rebuilt after a container recreate) — fine for favicons. A persistent
cache would require making the data-volume dir writable by the root worker
(`chown` it to `0:0` or `chmod 0777`), which fights the image's own startup.

Diagnosing worker writability: check the **worker** uid (`cat /proc/<worker>/status`),
not `docker exec id` (which defaults to root), and test the real path with
`docker exec searxng python3 -c "import sqlite3; sqlite3.connect('<path>')…"`.

## Prevention

- Never pin the full SearXNG `settings.yml`; use `use_default_settings` plus a
  small override block.
- A healthy container ≠ working search. After any config change, hit
  `/search?q=X` and assert **HTTP 200** — a template/render error (e.g. favicons)
  only surfaces on an actual query, not in the healthcheck.
- Keep service config templated to the **actual bind-mount path**, with
  recreate-on-change wired to the template's `.changed`.
- Keep secrets in ansible-vault, gated by `is defined and | length > 0`; never
  read-from-host.
- Verify after deploy:
  - `/config` — loaded engines + autocomplete backend.
  - `/search?q=X&format=json` — check `unresponsive_engines` and per-result `engines`.
  - `/autocompleter?q=` — confirms the autocomplete backend.
  - Prove the Brave key independently of SearXNG:
    ```bash
    curl -H "X-Subscription-Token: $KEY" "https://api.search.brave.com/res/v1/web/search?q=test"
    ```
    `200` = valid key; `422` points at a request-parameter issue (e.g. `count > 20`),
    not a bad key.

## Related

- Issues #50, #57 (and PR #56).
- [docker-compose shared external network survives recreate](./docker-compose-shared-network-subnet-recreate.md) — sibling "config drift surfaces only on a real deploy" lesson.
- [LMS custom-init & image-extension hooks](../conventions/lms-custom-init-and-image-extension-hooks.md) — same "extend the upstream image via its own merge/hooks, don't fork the whole catalog" philosophy.
- [Ansible change-loop pitfalls](../conventions/ansible-change-loop-pitfalls.md) — idempotency gates and verify-live-before-merge.

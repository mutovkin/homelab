# Nginx Proxy Manager → Ansible-managed Docker on eq12_docker (#331) Implementation Plan

> **For agentic workers:** follow `.claude/skills/homelab-change-loop/SKILL.md`. Branch
> `feat/331-npm-docker` off master; PR titles end `(#331)`; squash merge. Phases 0, 1, 4
> and 5 are separate trips through the loop (separate PRs). Phases 2 and 3 are operator
> steps, recorded on the issue.
>
> **Operator decisions (2026-11-14):** CT 104 is **never deleted**. After cutover it is
> stopped and set to not autostart (`onboot: 0`), and kept as a cold standby. The
> domain and all other private values stay in ansible-vault (#333; `task lint:vault-leaks`).
>
> **Review history:** v1 was reviewed 2026-11-14 by an independent agent (verdict SOUND
> WITH CHANGES; 12 findings, each verified against the live hosts). This v2 folds all of
> them in. Findings that change behaviour are marked **[R#n]**.

**Goal:** Replace CT 104 (an NPM 2.12.6 helper-script LXC that nothing manages) with
`jc21/nginx-proxy-manager` running as a `services/npm` compose role on eq12_docker
(CT 101). Carry over the existing DB, proxy hosts and Let's Encrypt state unchanged, so no
proxy host is recreated by hand. Then upgrade to the current release.

**Why:** #329. The wildcard cert NPM served was expired from 11-08 to 11-14, because
tmpfiles aged out `/tmp/nginx/body` and every reload failed `nginx -t`. Nothing alerted.
CT 104 had no unattended-upgrades, its apt lists were stale since February, and its
NodeSource and OpenResty repos fail `sqv` signature checks. The original reason for the
custom LXC (a 1&1 DNS certbot plugin) is gone: the domain is on Cloudflare, which the stock
image supports.

## Live facts this plan is pinned to (2026-11-14, re-verify in Phase 1 Task 1)

| Fact | Value |
| ---- | ----- |
| NPM on CT 104 | 2.12.6, openresty 1.27.1.2, node v22.21.0. The certbot venv was upgraded past the 2.12.6 image's (renewal conf records `version = 5.1.0`), so "like-for-like" holds for NPM, **not for certbot** [R#6] |
| Image `2.12.6` | `6ab097814f54`. **Declares no HEALTHCHECK** and only `/data` as a volume; entrypoint `/init` (s6); certbot at `/opt/certbot/bin`; `/usr/bin/check-health` exists [R#4] |
| Image latest | `2.17.0` (`91277490a919`). 2.13.3 notes say its migration is not easily reversible. 2.15.0 bumps certbot to 5.9.0. **From 2.16.0 the DNS credentials file is deleted after every certbot run** (credentials live only in the DB) [R#5, R#6] |
| State | `/data` 1.1 MB (`database.sqlite` 286 KB), `/etc/letsencrypt` 363 KB. DB `meta` holds the Cloudflare token; `credentials-7` is 0600 root |
| Renewal timing | Hourly tick plus one run at boot; renews certs within 30 days of expiry (`renewBeforeExpirationBy`). Cert 7 `expires_on` 2027-01-07 16:21:35, so the window opens **2026-12-08** |
| Proxy hosts | 13, all `ssl_forced`, http2 and websockets; no access lists, streams or redirections. One has an advanced config (the VictoriaMetrics UI) |
| Leftovers | `live/npm-4` (no archive, ignored by certbot), `proxy_host/4.conf.bak`. Not matched by `*.conf`, so excluding them is optional and is not asserted [R#11c] |
| DNS | `*.<domain>` is a CNAME to the apex; apex A = **192.168.25.20**, TTL 900, including via 1.1.1.1. Private IP, so NPM is **LAN-only**; DNS-01 needs no inbound traffic |
| CT 104 units | `npm` and `openresty` are **enabled**; the CT is `onboot: 1` [R#7] |
| CT 101 | `192.168.25.15`; ports 80, 81 and 443 are free; **no `sqlite3` binary** [R#5]; containers resolve DNS via `127.0.0.11` → router `192.168.23.1` |
| Subnets | eq12 uses 172.20–.25 (live), and 172.18/.19 is the default pool. n5pro pins 172.26–.31. **part of `172.16.0.0/16` is routed LAN space**: the UniFi upstream resolves into it [R#2] |
| Backups | No vzdump job exists on eq12. `/data/backups` on CT 101 sits on the same disk [R#10] |

### Upstreams (unchanged by the move)

This repo is **public**, so the per-host list of subdomains and upstreams is deliberately
**not recorded here**. Wildcard DNS means it can't be enumerated publicly, and this file
shouldn't do it for anyone. Phase 1 Task 1 reads the live list from CT 104's database
(`SELECT domain_names, forward_scheme, forward_host, forward_port FROM proxy_host WHERE
is_deleted=0`) into the operator's working notes, and the Phase 2 matrix runs from that
list. The categories it contains: docker services on CT 101 (`deb-docker.lan:<port>`), Home
Assistant, TrueNAS, the UniFi controller (HTTPS upstream), and one n5pro_docker service.

## Global constraints

- CLAUDE.md binds. Change hosts through Ansible only. Dry-run first. Both `security_opt`
  lines on every service. Pick a watchtower posture. Single-quote `$` values in `.env`.
  Secret-bearing tasks set `diff: false`. Never let a loop item carry a secret.
- The Cloudflare token is **migrated as data** (in NPM's DB, and in `credentials-7` until
  2.16) and is never templated from the repo. **There is no vault copy** [R#11a]: it would
  drift the first time someone rotates the token in the UI. DR is "restore the backup
  tarball".
- **Never run two NPMs that could renew the same cert.** The renewal window opens
  **2026-12-08**, and NPM also renews at **boot**. Every phase where both NPMs can run must
  finish before then, and at cutover CT 104's units are **disabled** as well as stopped
  [R#7].
- A guard you have not seen fail is not a guard. `--check` cannot exercise the seed, the
  firewall hairpin or the renewal; their first real execution is their first test, and the
  plan says so where it applies [R#3].

---

## Phase 0: served-cert expiry alert (#329 item 3). Lands FIRST, as its own PR

**Shipped and verified live 2026-11-14:**
- telegraf `[[inputs.x509_cert]]` probes `https://${SERVED_CERT_PROBE_HOST}:443`, a proxied
  name **by name** [R#8]. The value is templated from `vault_grafana_domain` (`:?`-guarded
  in compose), so the repo never carries it. Roots are excluded and it's tagged
  `check_type=served_cert`.
- Rules in `served-cert-health.yaml`:
  - `obs-served-cert-expiring`: `min by (check_type) (x509_cert_expiry) < 14d`.
  - `obs-served-cert-absent`: the `lag()` shape with `noDataState: Alerting`. It owns absence
    exactly once.
- Live: telegraf reads the leaf, and VictoriaMetrics shows 54.49 days left (to 2027-01-07).
  Both rules are served and inactive.
- **Falsified without paging anyone:** Grafana's `/api/v1/eval` runs a rule's queries and
  sends nothing.
  - Expiry rule: the real rule gives C=0; with the threshold at 60 days it gives C=1.
  - Absence rule: real data gives 0; a renamed (silent) metric gives NoData, which this
    rule treats as alerting; a threshold of 0 gives 1.
  - That proves both rules can fire. Delivery itself is already proven for this contact
    point by the earlier drills.

## Phase 1: `services/npm` role plus firewall hairpin fix, deployed but not yet serving traffic

**Files:** `ansible/roles/services/npm/{files/compose.yaml,templates/env.j2,tasks/main.yml,tasks/seed.yml,defaults/main.yml,README.md}`;
`ansible/roles/services/{portainer,vaultwarden}/tasks/main.yml`;
`ansible/inventory/host_vars/eq12_docker/vars.yml`;
`ansible/inventory/host_vars/n5pro_docker/vars.yml` (the fleet subnet map).

- [ ] **Task 1: Re-verify** every row of the facts table. Re-check that ports 80, 81 and 443
      are free (`ss -ltn`).
- [ ] **Task 2: Subnet** [R#2]. Pin `docker_networks.npm: "172.17.0.0/24"`. Extend the map's
      eq12 row to record it, and add a note that **172.16.x is off-limits** because part of
      it is routed LAN space (the exact /24 lives in the operator's notes, not here). A docker
      route for it would shadow the UniFi upstream. `env.j2` reads the subnet with `mandatory()`.
- [ ] **Task 3: compose.yaml.**
  - Image `jc21/nginx-proxy-manager:2.12.6`, pinned. While it's pinned, watchtower reports
    nothing (immutable tag); the README says so [R#5].
  - Ports `80:80`, `443:443`, `81:81`.
  - Volumes `/data/npm/data:/data` and `/data/npm/letsencrypt:/etc/letsencrypt`.
  - Both `security_opt` lines.
  - Labels `watchtower.enable=true` + `watchtower.monitor-only=true`.
  - **`healthcheck: test: ["CMD", "/usr/bin/check-health"]`** [R#4]. The image declares
    none, so ours is the only health state.
  - `restart: unless-stopped`; `npm_network`.
- [ ] **Task 4: Firewall hairpin fix** [R#1]. This is a prerequisite and was measured broken.
      Only tables included with `nft_fw_lan_iface` get the
      `iifname != "eth0" ip saddr 172.16.0.0/12 accept` rule. `postgres_fw` and
      `observability_fw` have it; `portainer_fw` and `vaultwarden_fw` do **not**. Measured
      2026-11-14: from a sibling container, `:9000` and `:8086` **time out**, while `:18080`,
      `:10080` and `:8428` answer. Add `lan_iface: eth0` to `portainer_firewall` and
      `vaultwarden_firewall` in eq12_docker host_vars, and pass `nft_fw_lan_iface` in both
      roles' `nft_scoped_fw` include (copy the postgres include). Check the n5pro_docker
      portainer table gets an equivalent value or stays unaffected. **Seen-to-fail baseline
      already exists** (the timeouts). The proof is the same probe from a container flipping
      to 200/302, then a **second** apply showing no change.
- [ ] **Task 5: nftables for NPM.**
  - :81 (admin UI) is allowlisted to the operator subnet `192.168.48.0/24` plus loopback,
    using the scoped-firewall pattern (prerouting -150 with the `fib daddr type != local`
    caveat).
  - 80/443 stay LAN-open, equivalent to today.
  - Verify from a **blocked** source.
- [ ] **Task 6: Seed** (`tasks/seed.yml`) [R#3]. A one-shot task, delegated to **`eq12`**.
      CT 104 is not in the inventory, and this keeps privkeys and the token off the operator
      machine.
  - Gate: `-e npm_seed_from_ct104=true` **and** the destination DB is absent.
  - Order the steps so **the one that consumes the gate signal runs last**:
    1. `pct exec 104 -- sqlite3 /data/database.sqlite ".backup /root/npm-seed.sqlite"`.
       This gives a consistent copy, and CT 104 has `sqlite3`.
    2. Tar `/etc/letsencrypt` and `/data/nginx` (excluding `logs/`) on CT 104, then
       `pct pull 104` and `pct push 101`, then unpack into the bind paths.
    3. **Last:** push and place `database.sqlite`.

    If the run dies before step 3, the gate is still armed and the next run retries. It can
    never start NPM on a DB whose confs point at a missing
    `/etc/letsencrypt/live/npm-7/fullchain.pem`.
  - Every task that handles the tarballs or the DB sets `diff: false`. Loop over **names or
    indices only**, never over items that carry the token.
  - Assert on the placed DB with python3's `sqlite3` module (CT 101 has no `sqlite3` binary):
    13 live `proxy_host` rows and 1 live certificate, **matched by domain name**, not only
    by count.
  - `--check` cannot exercise this, so do one **scratch run** first,
    `-e npm_seed_dest=/data/npm-seedtest`, and inspect it. Then the real one.
- [ ] **Task 7: Unconditional pre-deploy assert** [R#3]. Immediately before the `_deploy`
      include, and independent of the seed var, assert that `/data/npm/data/database.sqlite`
      exists. A routine `--tags npm` run on an empty directory must fail red rather than let
      NPM create a default admin and an empty schema. To see it fail once: run against an
      empty scratch dest and confirm the role refuses.
- [ ] **Task 8: Pre-upgrade backup gate** [R#5, R#11b]. Use the vaultwarden pattern
      (gated on an image diff), not a daily timer.
  - When the resolved image differs from the running one: stop the container, tar
    `/data/npm` into `/data/backups/npm-pre-<ts>.tar.gz`, run `gzip -t`, start the
    container, then deploy.
  - The restore check opens the archived DB with python3 and counts rows.
  - No `sqlite3` binary is needed: stop-and-copy is consistent.
- [ ] **Task 9: Deploy.** Dry-run, then a scratch seed run, then live with
      `-e npm_seed_from_ct104=true`. DNS still points at CT 104.
- [ ] **Task 10: First-start checks** in the container.
  - `nginx -t` passes. The seeded confs carry `listen [::]:443`, which `50-ipv6.sh` rewrites
    on each start; container IPv6 is enabled here. `DISABLE_IPV6=true` is the fallback only
    if this fails.
  - The health state is `healthy`.
  - The test that tells cases apart for `no-new-privileges`: `certbot plugins` lists
    `dns-cloudflare` after NPM installs it, and the Phase 2 staging dry-run succeeds. s6 and
    certbot run as root and nginx only drops privileges, so no setuid binary should be
    involved. That's inferred; the test decides.

## Phase 2: parallel verification (operator). Finish before **2026-12-08**

- [ ] **First** [R#9]: add `192.168.25.15` to Home Assistant's trusted proxies. **Measured
      2026-11-14:** a request with `X-Forwarded-For` from CT 101 gets **400**, from CT 104
      200. The live list is `[192.168.25.20/32, 172.30.32.0/23]` in `.storage/http`. The
      YAML `http:` block is ignored since HA 2026.8, so use the supported tool in the `ha`
      repo: `tools/ha_http_config.py apply --proxy 192.168.25.20 --proxy 192.168.25.15 --proxy 172.30.32.0/23`.
      It stages the change and HA restarts into it. Then re-run the 400 probe from CT 101
      (expect 200), then `promote` within 5 minutes. Unpromoted, HA reverts on its own,
      which is the safety net. The new NPM's upstream traffic is masqueraded to CT 101's LAN IP (per-bridge
      `masquerade` rules). Keep `.20` until Phase 5.
- [ ] Compare all 13 names on old and new:
      `curl -sk --resolve <name>:443:192.168.25.20` against `--resolve <name>:443:192.168.25.15`.
      Compare status code, `Location`, and the served cert's notAfter and SAN. Add a
      websocket upgrade probe on Home Assistant, Joplin and Grafana. **Portainer and
      Vaultwarden are the cases the Phase 1 Task 4 fix exists for**: if they 502 here, that fix didn't land.
- [ ] Log in through each backend with its own trust list: Home Assistant, TrueNAS, UniFi,
      Joplin (`X-Real-IP`, #323). The `--resolve` probes only prove what was actually
      exercised.
- [ ] Renewal proof: `certbot renew --dry-run --cert-name npm-7` inside the container
      (LE staging; it doesn't touch the live cert). On 2.12.6 `credentials-7` still exists on
      disk, so a hand-run dry-run works. Note the certbot version it ran with; the CT 104 venv
      was newer [R#6].

## Phase 3: cutover (operator; seconds of impact)

- [ ] Lower the apex A-record TTL from 900 s, at least 15 minutes ahead.
- [ ] Change the apex A record `192.168.25.20 → 192.168.25.15` (the wildcard CNAME follows).
      Flush any router cache.
- [ ] Run the **tagged cutover play** [R#7], `playbooks/npm-ct104.yml --tags cutover`. It runs
      `pct exec 104 -- systemctl disable --now npm openresty`, so an eq12 reboot during the
      window can't resurrect CT 104 and reopen the renewal race. Its sibling `--tags rollback`
      runs `enable --now`. Both directions live in the repo, not as ad-hoc commands.
- [ ] Verify with the real names and no `--resolve`: all 13 return as in Phase 2, the served
      cert comes from the new container, logins work through HA, Vaultwarden, Joplin and
      Grafana, and the Phase 0 alert is green.
- [ ] **Rollback:** A record back to `.20`, then `npm-ct104.yml --tags rollback`.

## Phase 4: image upgrade 2.12.6 → current (its own PR, about a week after cutover)

- [ ] Read the release notes from 2.13.0 through 2.17.0 in full. Flag the 2.13.3 one-way
      migration, 2.15.0's certbot 5.1 → 5.9, and 2.16.0's transient credentials file.
- [ ] The Task 8 pre-upgrade gate takes and verifies the backup. Record the rollback tag
      (`2.12.6` and its digest). **After 2.13.3 the rollback is the archive, not the tag.**
- [ ] Compose: pin **to the exact target version** (`jc21/nginx-proxy-manager:2.17.0`), not
      `:2` [R#5]. With `:2`, `pull: always` turns every later routine deploy into an
      unreviewed one-way migration of the single entry point. Bumps stay deliberate,
      watchtower-style, and a weekly release-watch job can report new versions.
- [ ] Renewal proof after the upgrade [R#6]. `credentials-7` no longer exists between runs,
      so a hand-run `certbot renew --dry-run` fails for an unrelated reason. Drive it through
      NPM's own path instead: `POST /api/nginx/certificates/7/renew` via the API. That's a
      real renewal, so do it only inside the 30-day window, or rely on the scheduled tick
      plus the Phase 0 alert. Alternatively, write the file from DB meta for a staging
      dry-run and delete it afterwards.
- [ ] Verify the landed version from the container, re-run the Phase 2 matrix, and add a
      bump-log row.

## Phase 5: retire CT 104 as a cold standby (operator decision: **never delete it**)

- [ ] At cutover (Phase 3) CT 104's `npm`/`openresty` units are already disabled and
      stopped. After the rollback window (about 14 days), **stop the CT itself and set
      `onboot: 0` through Ansible** via CT 104's guest definition in `host_vars/eq12/vars.yml`
      and `proxmox_guests`. If that role pins `update: false` for this field, use the
      explicit reconcile path it provides rather than a manual `pct set`. Verify with
      `pct config 104 --current` that `onboot: 0` is live and `pct status 104` reads stopped.
- [ ] Take `zfs snapshot rpool/data/subvol-104-disk-0@pre-standby-331` [R#10].
- [ ] Keep CT 104 in inventory, with a comment saying it is a stopped cold standby since
      #331. Do **not** run `destroy-guest.yml` against it. Keep `playbooks/npm-ct104.yml`,
      so reviving the standby stays a repo-driven step.
- [ ] Remove the `192.168.25.20/32` grants from eq12_docker host_vars and the role READMEs
      (reviving CT 104 as a rollback re-adds them in the same change). Remove `.20` from
      HA's trusted proxies with the same `ha` tool.
- [ ] Docs: `docs/architecture.md`, `docs/eq12.md` (the CT 104 section becomes "stopped
      cold standby, NPM moved to CT 101 in #331"), `README.md`.
- [ ] Close #329 and #331.

## Phase 6 (optional, separate issue)

- Move eq12 upstreams to docker-network names so ports such as Joplin's `0.0.0.0:22300`
  can close. This closes #323's `X-Real-IP` bypass.

## Risks and how each is caught

| Risk | Caught by |
| ---- | --------- |
| Portainer and Vaultwarden 502 after cutover (hairpin dropped) | Phase 1 Task 4 fix, measured seen-to-fail, plus the Phase 2 matrix |
| Docker subnet shadows UniFi (routed 172.16.x LAN range) | Phase 1 Task 2 pin to 172.17.0.0/24, plus the map note |
| CT 104 resurrected by an eq12 reboot | `disable --now` at cutover, then `onboot: 0` in Phase 5 |
| Partial seed leaves the DB pointing at missing certs | Seed ordering (DB last), plus the scratch run |
| Routine deploy on an empty dir creates a default admin | Unconditional pre-deploy assert (Task 7), seen to fail once |
| Container NPM can't renew | Task 10, the Phase 2 staging dry-run, the Phase 4 API renewal, and the Phase 0 alert |
| HA rejects the new proxy (400) | Phase 2 first step plus a real login |
| Two NPMs renew one cert (incl. renewal at boot) | The 12-08 deadline plus `disable --now` at cutover |
| Unreviewed one-way upgrade via `:2` plus `pull: always` | Exact-version pin plus the pre-upgrade backup gate |
| DNS change goes wrong silently | Phase 0 probes by name |

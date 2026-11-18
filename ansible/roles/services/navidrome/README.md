# services/navidrome: Navidrome music streaming

Navidrome is a Subsonic-compatible music server. It runs as a compose service on
n5pro_docker (CT 201), and NPM proxies it at `music.<domain>`. It serves two read-only
libraries, **Lossless** and **Compressed**; the sidebar library selector switches between
them or shows both, and Subsonic apps see them as two music folders.

Issue: #342. Plan and rationale: `docs/plans/2026-11-16-2035-feat-navidrome-music-streaming-plan.md`.

## What lives where

| Thing | Where | In the repo? |
| ----- | ----- | ------------ |
| Compose, env template, firewall, heal unit | this role | Yes |
| Library 1 (Lossless) path | `ND_MUSICFOLDER` in `files/compose.yaml` | Yes |
| Library 2 (Compressed), users, stars, play counts, playlists | Navidrome's SQLite DB, `{{ data_mount }}/navidrome/data/navidrome.db` | **No.** DB state, created in the UI |
| Scheduled backups | `{{ data_mount }}/navidrome/backup/navidrome_backup_*.db` (0700) | No |
| Pre-deploy backups | `{{ data_mount }}/navidrome/backup/predeploy/` | No |
| Password encryption key | `vault_navidrome_password_encryption_key`, n5pro_docker `vault.yml` | Encrypted |
| Last.fm application key and secret | `vault_navidrome_lastfm_api_key`, `vault_navidrome_lastfm_secret` | Encrypted |
| Each user's Last.fm link | Navidrome's DB (Personal > Scrobble to Last.fm) | No |
| Account usernames (leak sentinels only, read by no config) | `vault_navidrome_admin_username`, `vault_navidrome_listener_username` | Encrypted |
| NPM proxy host for `music.<domain>` | NPM's DB on eq12_docker | **No.** Created by hand |
| TrueNAS NFS shares | TrueNAS config | **No.** Created by hand, spec below |

The repo is public. Never write the domain, a username, or anything about the collection
(artist or album names, counts that identify it) in this repo, an issue or a PR. Check
issue and PR text with `task lint:vault-leaks -- --message-file <draft>` before posting.

## TrueNAS shares

Not managed by Ansible. One share per library, each exported to CT 201's host-only
address only. The role's preflight fails if either is missing from `showmount -e`.

| Field | Lossless | Compressed |
| ----- | -------- | ---------- |
| Path | `/mnt/vault/media/music/lossless` | `/mnt/vault/media/music/compressed` |
| Description | `Lossless Music for Docker LXC (RO)` | `Compressed Music for Docker LXC (RO)` |
| Networks | `10.99.99.3/32` | `10.99.99.3/32` |
| Hosts | none | none |
| Read Only | yes | yes |
| Maproot User / Group | `root` / `root` | `root` / `root` |
| Mapall User / Group | none | none |
| Security | default | default |

Measured from CT 201 on 2026-11-16 (`showmount -e 10.99.99.2`):

```text
/mnt/vault/media/music/compressed 10.99.99.3/32
/mnt/vault/media/music/lossless   10.99.99.3/32
```

There is no Mapall, so TrueNAS checks the container's own UID. The container runs as
`3000:3000`, the owner of the library files. The dataset-root share
(`/mnt/vault/media/music`) goes only to the Proxmox host for the music workbench and is
writable. Never add CT 201 to it: that would expose `originals/` and `audiobooks/` and
grant write access.

## Access control

Two independent locks stop anyone from creating the admin through NPM. Navidrome makes
whoever opens an empty instance the admin, through `POST /auth/createAdmin`, which needs
no authentication and has no rate limit.

1. **Staged firewall grant (Ansible).** `navidrome_firewall.always` (the operator subnet)
   is admitted in every state. `navidrome_firewall.staged` (`192.168.25.15`, eq12_docker)
   is admitted only when the role's in-container probe positively reads at least one
   user. A missing database, a stopped container, a probe error or a zero count all
   withhold it. NPM then answers with a 502 after its 90-second connect timeout (measured
   2026-11-16; the firewall drops silently, so nothing answers sooner).
2. **NPM refuses the endpoint (manual, in the proxy host's Advanced config).** Every
   request to `/auth/createAdmin` gets nginx's own 403, in every state.

Residuals, accepted:
- Any device in the operator subnet can claim an empty instance during a first install.
  The first-install steps below check who the admin is before NPM is granted.
- `192.168.25.15` is eq12_docker's own address, so lock 1 admits every bridged container
  there, and lock 2 is not in their path. If the data dir is lost while the grant stands,
  the control is the "at least one user" check in the restore and DR runbooks below.

Both locks are re-checked on every deploy: the role probes the user count before AND
after the deploy, and the firewall follows the post-deploy result, so a deploy that comes
up on an empty database withdraws NPM's grant in the same run. The post-deploy probe and
render run in an `always:` section, so the withdrawal happens even when a later check fails
the deploy. A withdrawn grant on a
running instance makes the observability `/ping` probe fail, which pages (critical,
after 5 minutes): that is the intended signal that someone must create or restore the
admin.

Lock 2 was probed with path variants on 2026-11-16. Every variant that reaches
Navidrome's handler (`/auth/createAdmin`, `/auth/./createAdmin`, a query string) gets
openresty's 403 through NPM; the trailing-slash, double-slash, case and encoded variants
never reach the handler (Navidrome answers 404/405).

Header authentication stays off: `ND_EXTAUTH_TRUSTEDSOURCES` is deliberately unset, and
the role fails if a `navidrome.toml` appears in the data dir. Public share links are off.

Login rate limiting is Navidrome's default (5 per 20s per client address). Behind NPM,
every remote client has NPM's address, so one app with a stale password can lock out the
others for 20-second windows. If phones suddenly cannot log in, look for one client with
a wrong password.

## First install

1. Dry-run, then apply: `task deploy:service -- --tags navidrome --limit n5pro_docker`.
   NPM is withheld; the run ends with a message saying the instance has no users.
2. Confirm the NPM cutover (#331 Phase 3): CT 104 is stopped and `<domain>` resolves to
   `192.168.25.15`. If not, stop before steps 3–4.
3. In NPM, create the proxy host:
   - domain `music.<domain>`, forward `http` to `n5pro-docker.lan:4533`;
   - the existing wildcard certificate, Force SSL and HTTP/2 on;
   - in Advanced, paste:

     ```nginx
     location = /auth/createAdmin {
         return 403;
     }
     proxy_buffering off;
     proxy_set_header Remote-User "";
     ```

   Confirm in NPM's generated `proxy_host/<id>.conf` that the `location` block landed in
   the server for `music.<domain>`. Measured 2026-11-16: it does, and it answers 403. The
   `Remote-User` line does NOT take effect: NPM's `location /` includes `proxy.conf`, which
   sets its own proxy headers, so nginx does not inherit the server-level one. It stays as
   a marker of intent. The real control is the unset trusted-sources setting, proved by
   the forged-header test.
4. From outside CT 201, `music.<domain>` must return a 502 (after about 90 seconds), and
   a `POST` to `/auth/createAdmin` must return openresty's 403 page (HTML, not Navidrome's
   JSON). Both measured 2026-11-16.
5. From the operator subnet, open `http://192.168.30.15:4533`. The **Create Admin** page
   must appear. If a login page appears instead, someone else claimed it: stop, wipe
   `{{ data_mount }}/navidrome/data`, redeploy and start again. Create the admin with a
   long random password, then confirm the user count is exactly 1:
   `docker exec navidrome sqlite3 -readonly /data/navidrome.db 'select count(*) from user'`.
   Navidrome logs the new username at WARN, so it reaches VictoriaLogs (private, never
   the repo).
6. Settings → Libraries → **+**: name `Compressed`, path `/music/compressed`. Then create
   one non-admin listener account with both libraries for phone and desktop apps, so the
   admin login is never stored in a client. Add both usernames to the vault as the leak
   sentinels above. Pick usernames that are at least 6 characters and appear nowhere in
   the repo as ordinary words, or the leak guard fails on legitimate files.
7. Re-run the deploy. NPM is now granted, and `music.<domain>` shows the login page.
8. Deploy the observability telegraf probe: `task deploy:service -- --tags observability --limit eq12_docker`.

## Libraries

- Library 1 (Lossless) is `ND_MUSICFOLDER`. Navidrome rewrites library 1's path from it
  on **every** start, so changing it silently repoints the library and every Lossless
  track goes missing.
- Change a library's path by **editing** the library in the UI, never by deleting and
  re-adding it. Deletion loses its stars and play counts for good.
- The same album in both libraries is two albums, with separate stars and play counts.
- Never use **Remove missing files** (UI or `navidrome missing`) while a share is down
  or degraded. `ND_SCANNER_PURGEMISSING=never` keeps annotations on missing tracks until
  the share returns; a manual purge throws them away.

### Scan exclusions

Navidrome has no config setting to exclude folders. It reads a `.ndignore` file
(gitignore syntax, case-sensitive) in a folder and applies it to everything below.
The role keeps one at the root of each library, from `navidrome_scan_ignore` in
`defaults/main.yml`: the top-level `/_attic`, plus the exact names of the cue, log,
scan and artwork folders (`extras`, `scans`, `Covers`, `Artwork`, `INFO`, ...). This is
Lyrion's `ignoreDirRE: "^extras$"`, widened.

- The shares are read-only inside the container, so the role writes the files through
  the Proxmox host's read-write mount of the music dataset (`/mnt/nfs/music`). It then
  reads them back from inside the container. The host mount is needed only when a file
  has to change, and the role refuses to write unless the mount is live NFS.
- A `.ndignore` with no pattern line makes Navidrome ignore the **whole library**, and
  so does a single `*` or `/`. The role therefore accepts only literal folder names:
  every entry must match `^/?[A-Za-z0-9_ .-]+$`, and the list must not be empty.
- **No trailing slash.** Navidrome tests a folder as `Artist/Album/scans`, without a
  slash, so a `scans/` pattern hides only the files inside: the folder is still walked
  and stays listed (measured: 1129 such folders after a full scan). `scans` skips it.
- Excluded tracks already in the database become *missing*; they do not disappear.
  `PURGEMISSING=never` keeps them until an admin removes them under **Missing Files**.
  Do that only while both shares are healthy (see above).
- **Exact names only.** Album folders are always `<year> - <title>`, so a bare name is
  never an album. A substring pattern (`*scan*`, `*cover*`) would hide real albums
  whose titles contain the word. Before adding a name, count the audio files under it.
  An artist folder with exactly that name would be hidden too.
- Navidrome's matcher translates only `.`, `*` and `?` (and `?` matches a literal `?`).
  Every other character goes into a Go regexp unescaped. The literal-name rule above
  keeps those characters out, so one-off folders with `+`, brackets or parentheses in
  their names stay scanned.

`ND_AUTOIMPORTPLAYLISTS=false`: the library's `.m3u` files are rip and tool artefacts
whose paths don't resolve inside the container. With import on, 0.64.2 created 426
playlists, all of them empty. Turning import off does not remove playlists that already
exist; delete those in the UI.

## Backups and rollback

- Scheduled: 03:00 local, 14 kept, in `backup/`. The role asserts all three
  `ND_BACKUP_*` settings, and on a claimed instance it fails the deploy if the newest
  scheduled backup is older than 26 hours.
- Pre-deploy: when an Ansible deploy would change the image, the role first takes a
  backup into `backup/predeploy/` (through the running binary, or a cold copy of the
  database and its `-wal`/`-shm` if the container is stopped). It keeps the newest 5.
- Backups hold only the database. They need the same `ND_PASSWORDENCRYPTIONKEY` to be
  usable. **Never rotate that key.** A wrong key does not stop the server: it starts,
  `/ping` answers 200 and the healthcheck stays green. The only sign is `Could not
  decrypt` errors in the startup log (measured in the restore drill). Stored user
  passwords use the same key, so logins can be expected to fail too.
- Inspect a backup on a **copy**: even `sqlite3 -readonly` leaves `-wal`/`-shm` files
  beside it.
- **Not DR.** CT 201's `/data` is outside vzdump, and the backups share its disk. Losing
  that disk, or rebuilding CT 201, loses every star, play count and playlist.

### Rollback after a bad update

The order keeps the old binary from ever starting against the migrated database.

1. Find the previous exact version from Navidrome's last startup log line before the
   update, in VictoriaLogs, and cross-check it against the registry tag list. The
   Watchtower mail's digest identifies the **new** image, not the one to return to.
2. `systemctl disable --now navidrome-nfs-heal`, then `docker compose stop` in
   `{{ data_mount }}/deploy/navidrome`. Without the first command, a CT 201 reboot
   mid-rollback lets the boot heal start `:latest` (the broken image) on the restored
   database. Step 7's deploy re-enables the heal.
3. Copy `navidrome.db`, `navidrome.db-wal` and `navidrome.db-shm` aside.
4. Check the chosen backup: non-zero size and `PRAGMA integrity_check` returns `ok`. On
   0.64.2, `backup restore` accepts a file that is not a Navidrome database.
5. Restore with a one-off run of the old version, using the same binds, user and
   environment as the service (`docker compose run --rm` with the image overridden to
   `deluan/navidrome:<X.Y.Z>`), running `backup restore --backup-file <file>`.
6. Confirm at least one user exists.
7. Only then pin `deluan/navidrome:<X.Y.Z>` in `files/compose.yaml`, remove the
   watchtower label, and deploy.
8. Un-pin and restore the label once a fixed release ships.

### Disaster recovery

1. Restore the newest backup file into `{{ data_mount }}/navidrome/data/navidrome.db`
   **before the first start**, owned by `3000:3000`. Without the vault key it is unusable.
2. Confirm at least one user exists before starting the container. An empty instance
   would be claimable by any eq12_docker container while the staged grant stands.
3. Deploy. The role re-reads the user count and grants NPM only if it is at least 1.

## Go-live record (#342)

Measured live, 2026-11-16 to 2026-11-17. Counts only; nothing about the collection.

| Check | Result |
| ----- | ------ |
| Landed version | 0.64.2 |
| Watchtower scan scope on n5pro_docker | `scanned=4` (navidrome `enable` only; lms auto; portainer, watchtower monitor-only) |
| Converged re-run | `changed=0`, container ID and `.Created` unchanged |
| Staged grant, no users | NPM's address absent from `inet navidrome_fw`; curl from eq12_docker and the music workbench exits 28; NPM answers 502 after 90 s |
| Staged grant, admin exists | NPM's address present; `/` 302 to `https://music.<domain>/app/`, `/ping` 200, wildcard cert served |
| Lock 2 (NPM 403 on `/auth/createAdmin`) | openresty 403 for every variant that reaches the handler; others 404/405 at Navidrome |
| Forged `Remote-User` through NPM | native API 401; Subsonic `status: failed` |
| Share links | 404 |
| Claim probe fails closed | DB path broken: grant withdrawn, message says probe failed, no file created |
| Post-deploy revoke | claimed before, empty after (`-e navidrome_claim_probe_db_post=<nonexistent>`): grant withdrawn in the same run. Also with a post-deploy check failing in the same run (writer mount forced to a non-mount path, 2026-11-17): the play went red, the `always:` section still ran, and NPM's rule count went 1 to 0; the next normal run restored it and a second run was `changed=0` |
| Recovery of stopped / removed container | one run each: started, grant restored; removed case took a cold-copy backup first |
| Pre-deploy backup assert | backup pointed outside `predeploy/`: deploy refused; normal path: non-empty file from this run, assert passed. Re-proved after moving the start stamp to CT 201's clock (2026-11-17): refused when pointed outside `predeploy/`, passed on two real backups |
| Freshness assert | max age 0 h: refused at its own message |
| Export preflight | dataset root (exported to the Proxmox host only), a path prefix, and a lookalike client address all refused |
| Heal timeout path | bogus export, 20 s: exit 1 with "giving up" |
| Time zone | image ships no tzdata; host zoneinfo mounted; container now reports Pacific time |
| Same-extension overlap between libraries | 5 FLAC files in Compressed: the only files cross-library move detection could re-home |
| Last.fm | key and secret reach the container; operator linked an account and confirmed scrobbles arrive (2026-11-17) |
| Restore drill (R9), 2026-11-17 | manual backup, the 03:00 backup and the live DB: `integrity_check` ok, identical counts in 14 tables (user, library, user_library, media_file, album, artist, annotation, starred, play-count sum, playlist, playlist_tracks, scrobbles, user_props, player) and the same schema version. Restored copy started on a portless scratch container with the vaulted key: no migration, no decrypt errors. Positive control, same copy with a wrong key: `Could not decrypt` errors at startup |
| First scheduled 03:00 backup timestamp | `03:00:00` Pacific on 2026-11-17 |
| Scan exclusions (2026-11-17) | files written via the host mount, read back in the container, second run `changed=0`. Literal-name guard seen to fail on `*`, `/`, `**/`, `*scan*/`, `Info + Cover/`, an empty list, a blank entry and a comment entry (each stopped at the guard, `changed=0`); a non-mount writer path stops it before any write. After the patterns lost their trailing slash and a full scan (2026-11-18): every excluded-name folder (2767) marked missing and none of their subfolders live; all 5379 `_attic` tracks marked missing, then removed under Missing Files; 0 missing tracks remain; full scan 7m31s, down from 8m47s. The 426 empty imported playlists were deleted once by hand (file-imported and empty only, backup first); users, stars and plays unchanged |
| L2 bypass to the container bridge address (2026-11-17) | blocked by Docker 29.9.3's raw-table rule (`ip daddr <container> iifname != <bridge> drop`), which runs before this role's table. From the music workbench (same /18, not allowlisted) with a temporary route to the bridge subnet via CT 201: control `172.30.0.1:22` answered (route reaches CT 201), `<container>:4533/ping` timed out, and the drop counter went 0 to 5. Depends on Docker >= 28 and `allow-direct-routing` staying off |

## Changing a share

Docker fixes an NFS volume's `driver_opts` when it creates it. After changing an export
path or the address, `docker compose down`, remove the `navidrome_navidrome_lossless` or
`navidrome_navidrome_compressed` volume, then redeploy.

## Updates

`:latest` with Watchtower auto-update, by the operator's choice (#342). The exception and
its reasons are in `roles/services/watchtower/README.md`, "Update Posture". The deploy
prints the landed version. After any minor-version jump, re-check the forged-header and
share-link probes from the plan's go-live steps, since a default could have moved.

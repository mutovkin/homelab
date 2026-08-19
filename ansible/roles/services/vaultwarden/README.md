# Vaultwarden Password Manager

## Description

[Vaultwarden](https://github.com/dani-garcia/vaultwarden) is an unofficial [Bitwarden](https://bitwarden.com/)-compatible password manager server written in Rust. It provides a secure, self-hosted alternative to commercial password managers, offering all the core features of Bitwarden including password storage, secure note taking, two-factor authentication, and organization management with significantly lower resource requirements.

Key features:

- Bitwarden-compatible API and client support
- Password and secure note storage
- Two-factor authentication (2FA) support
- Organization and collection management
- Secure password sharing
- Web vault interface
- Emergency access functionality
- Send feature for secure temporary sharing
- SMTP integration for email notifications

## Data Folder Permissions

The Vaultwarden container requires write access to store vault data, attachments, and logs. Set up the data directory with appropriate permissions:

```bash
# Create the data directory
sudo mkdir -p /data/vaultwarden

# Set ownership to the Vaultwarden user (UID 1000)
sudo chown -R 1000:1000 /data/vaultwarden

# Set appropriate permissions
sudo chmod -R 755 /data/vaultwarden
```

The data directory will contain:

- SQLite database files (vault data)
- Attachment files (if file attachments are enabled)
- Log files (application logs)
- Configuration backups
- Send files (temporary secure sharing)

## Security Considerations

These are the controls the role actually enforces, not aspirations:

- **`ADMIN_TOKEN` is an Argon2id PHC hash, never the raw token (#81).** The container
  only ever holds `vault_vaultwarden_admin_token_hash` (Argon2id, t=3, m=65540 KiB, p=4 —
  vaultwarden's own `vaultwarden hash` default `bitwarden` preset), so `docker inspect`,
  `printenv`, the on-disk `.env`, and any backup of them expose a value that verifies a
  login but cannot produce one. You still log in to `/admin` with the *plain* token, which
  lives only in `vault_vaultwarden_admin_token_plain` in the encrypted vault and is
  templated nowhere. To rotate: change the plain var, re-hash it
  (`docker run --rm -it vaultwarden/server:latest /vaultwarden hash`, storing the
  `$argon2id$...` value without the `ADMIN_TOKEN='...'` wrapper it prints), and update the
  hash var in the same vault edit. `tasks/main.yml` asserts the value is a PHC string
  before deploying, because vaultwarden accepts a non-PHC value as a *plaintext* token
  rather than rejecting it. Every value in `templates/env.j2` is single-quoted: compose's
  dotenv parser interpolates `$VAR` in unquoted values, which would truncate the
  `$`-delimited PHC segments — emitting only an ungated stderr warning, then exiting 0.
  Full write-up:
  [docs/solutions/security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md](../../../../docs/solutions/security-issues/vaultwarden-admin-token-dollar-truncation-and-plaintext-fallback.md).
- **tcp/8086 is restricted to the reverse proxy and operator workstations (#81).** The
  port stays published — NPM runs in a separate LXC and reaches it over the LAN — but the
  role ships a fail-open nftables table `inet vaultwarden_fw` that drops that one port for
  everything except 192.168.25.20 (NPM), 192.168.48.0/24 (operators), and loopback. It
  hooks `prerouting` at priority -150, before Docker's DNAT at -100, because an input-hook
  rule is *invisible* to docker-published ports; see
  [docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md](../../../../docs/solutions/integration-issues/nftables-input-hook-inert-for-docker-published-ports.md).
  The table is built by the shared `roles/nft_scoped_fw` (#114) from the per-port
  allowlist `vaultwarden_fw_ports` in `defaults/main.yml`. Every real (non-check) deploy
  probes the kernel for the table, reloads inline when it is missing or the ruleset
  changed, and hard-asserts — a `RemainAfterExit` oneshot reports "active" even after an
  external `flush ruleset`, so unit state proves nothing.
- **Pre-upgrade backups are gated on the vault data, not the container (#107).** Whenever
  `/data/vaultwarden` holds data and the deploy would hand it to a different image —
  including a container-absent start after a `docker rm` — the role stops the container
  (if any), archives the data to `{{ data_mount }}/backups/vaultwarden-<ts>.tgz`, and
  prunes to `vaultwarden_backup_retention` (default 7, asserted >= 1). A failed backup
  discards a partial archive, restarts the vault (if any) on its existing image, and
  aborts with the underlying error rather than upgrading unbacked. A routine no-op deploy
  stops nothing and archives nothing. The probe distinguishes "no data directory" from
  "directory unreadable" and fails on the latter — silently reading an unmountable vault
  as "empty" is how an upgrade goes unbacked.
- Traffic from clients arrives over TLS via the NPM reverse proxy; 8086 itself is plain
  HTTP and must never be exposed beyond the allowlist above.
- Monitor `/data/vaultwarden/vaultwarden.log` for `Invalid admin token` and other
  suspicious access attempts.

The service runs on port 8086 and provides the web vault interface for user access and administration.

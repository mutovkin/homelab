# NextCloud

Self-hosted file sync, share, and collaboration platform.

## Services

| Service | Port | Purpose |
|---|---|---|
| nextcloud | 8080 | Web UI (Apache) |
| nextcloud-redis | 6379 (internal) | Session and file locking cache |

## Dependencies

- **PostgreSQL**: Uses the shared PostgreSQL instance on this host (database: `nextcloud`)
- **SMTP**: Gmail for email notifications

## Configuration

After first deploy, complete the setup wizard at `http://<host>:8080`.
Admin credentials are injected via environment variables on first run.

For production use behind a reverse proxy, set `NEXTCLOUD_TRUSTED_DOMAINS` and
configure `overwrite.cli.url` in NextCloud's `config.php`.

# Watchtower Container Updater

## Description

[Watchtower](https://containrrr.dev/watchtower/) is an automated Docker container update service that monitors running containers and automatically updates them when new image versions are available. It checks for updated images on a scheduled basis, gracefully stops the old container, pulls the new image, and starts the updated container with the same configuration.

Key features:

- Automatic container updates on schedule
- Label-based selective updating
- Rolling restart support to minimize downtime
- Email notifications for update status
- Cleanup of old images after updates
- Configurable update schedules
- Support for private registries
- Graceful container shutdown with timeout

## Data Folder Permissions

Watchtower does not require any persistent data directories as it operates entirely through the Docker socket. It only needs access to:

```bash
# Docker socket access (handled automatically by Docker)
# No additional permissions needed for /data directories

# The only requirement is Docker socket access:
# /var/run/docker.sock:/var/run/docker.sock

# No data folder setup required
```

Watchtower is stateless and does not persist any data to the filesystem. All configuration is handled through environment variables, and it communicates with Docker through the Docker socket.

## Configuration Notes

- Runs on schedule: 4:30 AM daily (configurable via `WATCHTOWER_SCHEDULE`)
- Only updates containers with the label `com.centurylinklabs.watchtower.enable=true`
- Automatically cleans up old images after successful updates
- Sends email notifications about update results
- Uses rolling restart to minimize service interruption
- Includes a 30-second timeout for graceful container shutdown

The service requires no data persistence and operates entirely through Docker API calls via the mounted Docker socket.

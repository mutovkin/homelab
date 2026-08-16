# Portainer

## Description

[Portainer](https://www.portainer.io/) is a lightweight management UI for Docker environments. It provides a web-based interface to manage Docker containers, images, volumes, networks, and stacks. Portainer simplifies Docker management by offering an intuitive dashboard that allows you to monitor container status, view logs, access container terminals, and deploy applications through a user-friendly interface.

Key features:

- Web-based Docker management interface
- Container lifecycle management (start, stop, restart, remove)
- Image management and registry integration
- Volume and network management
- Stack deployment with Docker Compose
- User access control and team management
- Real-time monitoring and logging
- Terminal access to containers

## Data Folder Permissions

Portainer uses a Docker named volume (`portainer_data`) rather than a host directory mount. However, if you need to access the data directly, the volume is typically stored in Docker's volume directory.

To check volume location:

```bash
# Find the volume location
docker volume inspect portainer_data

# If you need to backup or access the data
sudo ls -la /var/lib/docker/volumes/portainer_data/_data/
```

No special permissions setup is required as Docker manages the volume automatically. The container runs with appropriate permissions to access its data volume.

If you prefer using a host directory instead of a named volume, you can modify the compose file and set permissions:

```bash
# Create the data directory
sudo mkdir -p /data/portainer

# Set ownership to the Portainer user (UID 1000)
sudo chown -R 1000:1000 /data/portainer

# Set appropriate permissions
sudo chmod -R 755 /data/portainer
```

## Configuration

The service provides:

- Web interface on port 9000 (HTTPS)
- Edge agent communication on port 8000
- Direct Docker socket access for container management

Access Portainer at `https://localhost:9000` after first startup to complete the initial setup and create an admin user.

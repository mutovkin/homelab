# Joplin Server

## Description

[Joplin](https://joplinapp.org/) [Server](https://github.com/laurent22/joplin) is a synchronization service for the Joplin note-taking application. It provides a centralized server where Joplin clients can sync notes, notebooks, tags, and attachments. This server enables secure note synchronization across multiple devices while maintaining end-to-end encryption of note content.

Key features:

- Note synchronization across devices
- End-to-end encryption support
- Web clipper integration
- Email verification and user management
- Filesystem and database storage options
- SMTP configuration for notifications

## Data Folder Permissions

The `/data/joplin` folder must be accessible by the container's user (UID 1001). Set the correct permissions:

```bash
# Create the data directory
sudo mkdir -p /data/joplin

# Set ownership to the Joplin user (UID 1001)
sudo chown -R 1001:1001 /data/joplin

# Set appropriate permissions
sudo chmod -R 755 /data/joplin
```

The container uses filesystem storage for better performance, storing note data and attachments in the mounted volume. Ensure the directory is writable and has sufficient disk space for your notes and attachments.

## Configuration

Before starting the service:

1. Create environment file with database and SMTP settings
2. Ensure PostgreSQL is running and accessible
3. Configure the data directory permissions as shown above
4. The service will be available on port 22300

The server integrates with PostgreSQL for metadata storage while using the filesystem for efficient file storage.

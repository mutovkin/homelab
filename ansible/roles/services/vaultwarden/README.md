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

- Ensure regular backups of the `/data/vaultwarden` directory
- Use strong admin tokens and SMTP passwords
- Consider placing behind a reverse proxy with SSL/TLS
- Regularly update the container for security patches
- Monitor logs for suspicious access attempts

The service runs on port 8086 and provides the web vault interface for user access and administration.

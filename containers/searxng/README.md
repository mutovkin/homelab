# SearXNG Docker Compose Setup

## Description

[SearXNG](https://docs.searxng.org/) is a free internet metasearch engine that aggregates results from various search engines without storing user data or tracking users. It provides privacy-focused search capabilities by acting as a proxy between users and search engines, removing tracking and ads while delivering comprehensive search results from multiple sources.

Key features:

- Privacy-focused metasearch engine
- No user tracking or data storage
- Aggregates results from 70+ search engines
- Customizable search categories and engines
- No ads or sponsored results
- Self-hosted for complete privacy control
- Multiple output formats (HTML, JSON, CSV, RSS)
- Configurable rate limiting and security
- Useful for local LLM MCP servers when they need to search internet

## Data Folder Permissions

SearXNG requires write access to its configuration and cache directories. Set up the data directories with appropriate permissions:

```bash
# Create the data directories
sudo mkdir -p /data/searxng/config
sudo mkdir -p /data/searxng/data

# Set ownership to the SearXNG user (UID 977)
sudo chown -R 977:977 /data/searxng

# Set appropriate permissions
sudo chmod -R 755 /data/searxng
```

The data directories will contain:

- `/data/searxng/config`: SearXNG configuration files (settings.yml, etc.)
- `/data/searxng/data`: Cache data and temporary files

**Note:** The configuration directory will be automatically populated with default configuration files when the container first starts if it's empty.

## Quick Start

1. **Generate a secret key**:

   ```bash
   openssl rand -hex 32
   ```

2. **Create environment file**:

   ```bash
   cp .env.searxng .env
   ```

   Edit `.env` and set your `SEARXNG_SECRET` and `SEARXNG_HOSTNAME`.

3. **Create the configuration directory**:

   ```bash
   mkdir -p searxng
   ```

4. **Access SearXNG**:
   - Open http://localhost:8080

## Configuration

The `searxng` directory will be automatically populated with default configuration files when the container first starts.

To customize SearXNG, edit the files in `./searxng/` directory after the first run:

- `settings.yml` - Main configuration file
- `limiter.toml` - Rate limiting configuration

## Port Configuration

Default port: `8080`

To change the port, modify this line in `searxng.yml`:

```yaml
ports:
  - "8080:8080"  # Change first number to your desired port
```

## Reverse Proxy

If using behind a reverse proxy (Nginx, Traefik, Caddy):

1. Set `SEARXNG_BASE_URL` to your public URL
2. Configure your reverse proxy to forward to port 8080
3. Remove or comment out the `ports` section in the compose file

## Security Notes

- The container runs with minimal capabilities (cap_drop: ALL)
- Only necessary Linux capabilities are added back
- Always use a strong random secret key
- Consider using HTTPS in production

## Updating

To update SearXNG:

```bash
docker-compose -f searxng.yml pull
docker-compose -f searxng.yml up -d
```

Or use Portainer's "Update" button in the stack view.

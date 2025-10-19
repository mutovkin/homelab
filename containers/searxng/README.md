# SearXNG Docker Compose Setup

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

4. **Deploy with Portainer**:
   - In Portainer, go to "Stacks" → "Add stack"
   - Name it "searxng"
   - Upload or paste the contents of `searxng.yml`
   - Add environment variables from `.env` file
   - Click "Deploy the stack"

5. **Access SearXNG**:
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

# Homelab Container Stacks

## Repository Guide

### Initial Setup for Git Safety

This repository is configured to be git-safe - all sensitive data is stored in `.env` files which are excluded from version control.

**Steps to set up each service:**

1. Navigate to the service directory (e.g., `watchtower/`, `joplin/`, etc.)
2. Copy `.env.example` to `.env`:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and fill in your actual credentials and configuration
4. Never commit `.env` files - they're already in `.gitignore`

### Service-Specific .env Files

Each service has its own `.env` file:

- `joplin/.env` - Joplin server and database config
- `postgresql/.env` - PostgreSQL and PgAdmin credentials
- `searxng/.env` - SearXNG hostname and secret key
- `vaultwarden/.env` - Vaultwarden domain, admin token, SMTP
- `watchtower/.env` - Email notification settings

## Docker Compose Environment Variables

### Environment Variable Syntax

All files use **mapping syntax** (recommended for Portainer):

```yaml
environment:
  VARIABLE_NAME: ${VARIABLE_NAME}
  ANOTHER_VAR: ${ANOTHER_VAR:-default-value}
  STATIC_VAR: "static-value"
```

### Syntax Options

Docker Compose supports two syntaxes for defining environment variables:

#### 1. Array Syntax (with dashes)

```yaml
environment:
  - NODE_ENV=production           # Sets to explicit value
  - DATABASE_URL                  # Pulls from host environment
  - API_KEY                       # Pulls from host environment
```

#### 2. Mapping Syntax (key-value pairs)

```yaml
environment:
  NODE_ENV: production            # Sets to explicit value
  DATABASE_URL: ${DATABASE_URL}   # Pulls from host using interpolation (works with Portainer)
  API_KEY: ""                     # Empty value
```

### Best Practice with Portainer

**⚠️ Important:** When using Portainer's Stack feature:

1. **Use explicit values or `.env` files** - Portainer stacks don't inherit the host shell environment
2. **Avoid bare variable names** like `- DATABASE_URL` without values - these won't work in Portainer

#### Example for Portainer

```yaml
# ✅ GOOD - Works in Portainer
environment:
  DATABASE_URL: ${DATABASE_URL}   # Will use Portainer's env vars
  NODE_ENV: production
  
# ❌ BAD - Won't work in Portainer
environment:
  - DATABASE_URL                   # No host environment to pull from
```

#### Using .env files with Portainer

```yaml
# In docker-compose.yml
env_file:
  - stack.env

# In stack.env
DATABASE_URL=postgresql://user:pass@host:5432/db
API_KEY=your_api_key_here
```

## Watchtower Auto-Updates

Watchtower is configured with **label-based updates** for controlled, selective auto-updating.

### How it works

- Watchtower runs daily at 4:30 AM (America/Los_Angeles)
- **Only containers with the label will be auto-updated**
- Containers without the label will be monitored but NOT auto-updated

### To enable auto-update for a container

Add this label to the service in its docker-compose file:

```yaml
services:
  your-service:
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
```

### Recommended workflow

1. By default, don't add the label (manual updates only)
2. Monitor email notifications for new image versions
3. After a new version has been stable for a few days, decide:
   - Manually update and test, OR
   - Add the label to enable auto-updates for that container
4. Only enable auto-updates for containers you trust

### Email notifications

- Watchtower sends email notifications about available updates
- Check these regularly to stay informed about new versions

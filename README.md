# Homelab Server Configuration

## Status

Work in progress to document my current setup so that I can reproduce it if needed.

## Hardware

- Beelink EQ12 Pro
  - 16GB RAM
  - TEAMGROUP MP34 2TB SSD
  - 4 CPU Cores

## Proxmox Virtual Environment

### Node Configuration

- **Node**: pve
- **CGroup Mode**: 2

### Virtual Machines (QEMU/KVM)

#### VM 100 - Home Assistant

- **Type**: QEMU VM
- **Memory**: 4.0GB allocated
- **CPU**: 2 cores
- **Disk**: 32GB allocated
- **Tags**: proxmox-helper-scripts

### Containers (LXC)

#### Container 101 - deb-docker

Docker containers runtime environment.
Decided to go with LXC approach in order maximize flexibility of limited RAM.
In VM I would need to allocate specific amount of memory while in LXC it is more flexible

- **Type**: LXC Container
- **OS**: Debian-based
- **Memory**: 4.0GB allocated
- **CPU**: 2 cores
- **Disk**: 8.0GB allocated

#### Container 102 - ubuntu-docker

- **Type**: LXC Container
- **OS**: Ubuntu-based
- **Memory**: 1.0GB allocated
- **CPU**: 1 core
- **Disk**: 8.0GB allocated

#### Container 103 - nginxproxymanager

Tried to configure NGINX Proxy Manager within a Docker container running within `deb-docker`,
however it always failed to run, so had to resort to using proxmox-helper-scripts.

- **Type**: LXC Container
- **Memory**: 1.0GB allocated
- **CPU**: 2 cores
- **Disk**: 4.0GB allocated
- **Tags**: proxmox-helper-scripts

### Storage Configuration

#### Local Storage (local)

- **Type**: Directory storage
- **Content Types**: ISO images, Container templates, Backups
- **Location**: `/var/lib/vz`

#### Local ZFS (rpool)

- **Type**: ZFS Pool
- **Content Types**: Container root directories, VM disk images
- **Pool Name**: rpool
- **Deduplication**: Disabled
- **Compression**: Enabled (default)

#### ZFS Dataset Structure

**Root Filesystem (`rpool/ROOT`)**

- **rpool/ROOT/pve-1**: Proxmox host OS (mounted at `/`)

**VM and Container Data (`rpool/data`)**

- **vm-100-disk-0**: Home Assistant VM boot disk
- **vm-100-disk-1**: Home Assistant VM main disk
- **subvol-101-disk-0**: Container 101 (deb-docker) root disk (6GB quota)
- **subvol-101-disk-1**: Container 101 (deb-docker) data disk (104GB quota)
- **subvol-102-disk-0**: Container 102 (ubuntu-docker) disk (8GB quota)
- **subvol-103-disk-0**: Container 103 (nginxproxymanager) disk (4GB quota)

### Network Configuration

- **SDN**: localnetwork (Software Defined Network)
- **Bridge**: vmbr0 (default Proxmox bridge)

### Resource Allocation Summary

- **Total VMs**: 1
- **Total Containers**: 3
- **Total Memory Allocation**: 10GB (VM + Containers)
- **Total Disk Allocation**: VM: 32GB, Containers: 25GB

## Container Services

Detailed configuration files and documentation for each container service can be found in the `containers/` directory:

- **[Joplin](containers/joplin/)** - Note-taking application server
- **[Observability](containers/observability/)** - Time Series and Logs aggregation and visualization
- **[Portainer](containers/portainer/)** - User-friendly container deployment
- **[PostgreSQL](containers/postgresql/)** - Database server with pgAdmin web interface
- **[SearXNG](containers/searxng/)** - Privacy-respecting search engine
- **[Vaultwarden](containers/vaultwarden/)** - Bitwarden-compatible password manager
- **[Watchtower](containers/watchtower/)** - Automatic container updates

## Docker Network Mapping

All containers use explicitly defined bridge networks with fixed subnets to prevent IP conflicts with the host LAN (192.168.x.x) and ensure predictable routing through Nginx Proxy Manager.

| Container Stack | Network Name | Subnet | Gateway | Containers |
|----------------|--------------|---------|---------|------------|
| **Observability** | `observability_network` | 172.20.0.0/24 | 172.20.0.1 | victoriametrics, victorialogs, vector, telegraf, grafana |
| **PostgreSQL** | `postgres_network` | 172.21.0.0/24 | 172.21.0.1 | postgres, pgadmin |
| **SearXNG** | `searxng_network` | 172.22.0.0/24 | 172.22.0.1 | searxng |
| **Portainer** | `default` | 172.23.0.0/24 | 172.23.0.1 | portainer |
| **Vaultwarden** | `vaultwarden_network` | 172.24.0.0/24 | 172.24.0.1 | vaultwarden |
| **Watchtower** | `watchtower_network` | 172.25.0.0/24 | 172.25.0.1 | watchtower |
| **Joplin** | `postgres_network` (external) | 172.21.0.0/24 | 172.21.0.1 | joplin-server |

### Network Design Notes

- **Subnet Isolation**: Each network uses a unique /24 subnet (254 usable IPs per network)
- **172.x.x.x Range**: All subnets use Docker's traditional private range to avoid LAN conflicts
- **Bridge Driver**: All networks use the bridge driver for container isolation with host NAT
- **Shared Networks**: Joplin connects to `postgres_network` to access the database
- **Gateway Assignment**: First IP (.1) in each subnet is the gateway

### Troubleshooting Commands

```bash
# List all Docker networks
docker network ls

# Inspect a specific network
docker network inspect observability_network

# Check container IP addresses
docker inspect <container_name> | grep IPAddress

# View all container IPs
docker ps -q | xargs docker inspect -f '{{.Name}} - {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

### Useful Proxmox Commands

```bash
# List all VMs
qm list

# List all containers  
pct list

# Get detailed resource information
pvesh get /cluster/resources --type vm --output-format json

# Check storage status
pvesm status

# Get complete cluster resource overview
pvesh get /cluster/resources --output-format=json
```

### ZFS Management Commands

```bash
# Check ZFS pool status and health
zpool status

# Show detailed pool information with device layout
zpool list -v

# Get all properties of the rpool
zpool get all rpool

# Show specific ZFS pool properties
zpool get compression,dedup,atime rpool

# List all ZFS datasets in the pool
zfs list -r rpool

# Show ZFS dataset properties
zfs get all rpool/data

# Check ZFS pool history (configuration changes)
zpool history rpool

# Upgrade pool to enable all features (CAUTION: may affect compatibility)
# zpool upgrade rpool

# Manually start a scrub (data integrity check)
zpool scrub rpool

# Check scrub status
zpool status -v rpool

# Show ZFS ARC cache statistics
cat /proc/spl/kstat/zfs/arcstats | grep -E "^(size|hits|misses|c_max)"

# Show ZFS module parameters
modinfo zfs | grep parm
```

#### ZFS Pool Configuration Notes

- **Feature Set**: Pool supports upgradeable features (use `zpool upgrade rpool` to enable)
- **Scrub Schedule**: Automated scrubbing enabled for data integrity verification
- **Compression**: LZ4 compression enabled by default
- **Snapshots**: Available for all datasets

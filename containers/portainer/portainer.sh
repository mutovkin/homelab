#!/bin/bash

# Portainer Docker Compose Management Script
# Navigate to the directory containing docker-compose.yml
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="portainer.yml"

case "$1" in
    start)
        echo "Starting Portainer..."
        docker-compose -f ${COMPOSE_FILE} up -d
        ;;
    stop)
        echo "Stopping Portainer..."
        docker-compose -f ${COMPOSE_FILE} down
        ;;
    restart)
        echo "Restarting Portainer..."
        docker-compose -f ${COMPOSE_FILE} restart
        ;;
    logs)
        docker-compose -f ${COMPOSE_FILE} logs -f
        ;;
    status)
        docker-compose -f ${COMPOSE_FILE} ps
        ;;
    backup)
        echo "Creating backup of Portainer data..."
        CURRENT_DATE=$(date -I)
        docker run --rm \
            -v portainer_data:/data \
            -v /data/portainer_backups:/backup \
            alpine tar cvzf /backup/portainer_data_backup_${CURRENT_DATE}.tar.gz /data
        echo "Backup created: portainer_data_backup_${CURRENT_DATE}.tar.gz"
        ;;
    upgrade)
        echo "Starting Portainer upgrade process..."
        
        # Stop the current Portainer service
        echo "Stopping Portainer service..."
        docker-compose -f ${COMPOSE_FILE} down
        
        # Backup the Portainer data volume
        echo "Creating backup of Portainer data..."
        CURRENT_DATE=$(date -I)
        docker run --rm \
            -v portainer_data:/data \
            -v /data/portainer_backups:/backup \
            alpine tar cvzf /backup/portainer_data_backup_${CURRENT_DATE}.tar.gz /data
        echo "Backup created: portainer_data_backup_${CURRENT_DATE}.tar.gz"
        
        # Pull the latest image and start the service
        echo "Pulling latest Portainer image and starting service..."
        docker-compose -f ${COMPOSE_FILE} pull
        docker-compose -f ${COMPOSE_FILE} up -d
        
        # Check if the container is running
        echo "Checking if Portainer is running..."
        sleep 5
        if docker-compose -f ${COMPOSE_FILE} ps | grep -q "Up"; then
            echo "✅ Portainer has been successfully upgraded and restarted."
            echo "Access Portainer at: http://localhost:9000"
        else
            echo "❌ Error: Portainer container failed to start."
            echo "Check logs with: docker-compose -f ${COMPOSE_FILE} logs"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|logs|status|backup|upgrade}"
        echo ""
        echo "Commands:"
        echo "  start   - Start Portainer service"
        echo "  stop    - Stop Portainer service"
        echo "  restart - Restart Portainer service"
        echo "  logs    - Show and follow Portainer logs"
        echo "  status  - Show current status"
        echo "  backup  - Create backup of Portainer data"
        echo "  upgrade - Upgrade Portainer to latest version with backup"
        exit 1
        ;;
esac
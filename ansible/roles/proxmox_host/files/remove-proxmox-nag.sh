#!/bin/bash
# Remove Proxmox subscription nag popup safely
set -e

FILE="/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js"

if [ -f "$FILE" ]; then
    # No-op if already patched — avoids unnecessary pveproxy restarts
    if ! grep -q "res\.data\.status\.toLowerCase() !== 'active'" "$FILE"; then
        exit 0
    fi

    # Get the current version of the toolkit package
    PVE_VER=$(dpkg-query -f '${Version}' -W proxmox-widget-toolkit)
    BAK_FILE="${FILE}.bak.${PVE_VER}"

    # Create a version-specific backup if it doesn't exist
    if [ ! -f "$BAK_FILE" ]; then
        cp "$FILE" "$BAK_FILE"
    fi

    # Restore from the *matching* version backup before patching
    # to prevent compounding patches or version mismatches
    cp "$BAK_FILE" "$FILE"

    # Match the exact line with proper context - only in the subscription check block
    sed -i "/checked_command.*function.*orig_cmd/,/^[[:space:]]*});$/ s/res\.data\.status\.toLowerCase() !== 'active'/false/g" "$FILE"

    echo "Proxmox subscription nag removed for version $PVE_VER: $(date)" >> /var/log/proxmox-nag-removal.log

    # Restart the web service so changes take effect
    systemctl restart pveproxy
fi

exit 0

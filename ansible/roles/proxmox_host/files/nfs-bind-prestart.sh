#!/bin/sh
# Proxmox guest hookscript (roles/proxmox_host, #254): gate a CT start on its
# NFS-backed bind mounts being LIVE on the host.
#
# Why: an unprivileged CT cannot mount NFS itself, so the host mounts the
# dataset and the CT gets `mpN: /mnt/nfs/<x>,mp=...` bind mounts. LXC binds
# whatever is at the source path when the CT starts; if the NFS mount is not up
# yet, that is an EMPTY directory, and the CT keeps that empty view for its whole
# life (private mount propagation) — a tag-processing job would then "succeed"
# against nothing. The host cannot mount at boot either: on n5pro the NFS server
# is VM 200 on this same host. So this script is the mount trigger, and exiting
# non-zero makes `pct start` refuse — the fail-safe direction.
#
# Called by Proxmox as: <script> <vmid> <phase>. Only pre-start acts.
# Bind sources OUTSIDE /mnt/nfs/ are ignored (local directory binds are not this
# script's business). A source UNDER /mnt/nfs/ that no configured mount covers is
# an error, not a skip: silence there would be exactly the fail-open this exists
# to prevent.
#
# Paths are word-split, so mountpoints and bind sources must not contain spaces.
set -eu

vmid="$1"
phase="$2"
[ "$phase" = "pre-start" ] || exit 0

list=/etc/proxmox-nfs-mounts.list
wait_s="${NFS_BIND_WAIT:-180}"
tag="nfs-bind-prestart[CT $vmid]"

# Bind-mount sources: `mpN: /abs/path,...,mp=/inside` (allocation-form volumes
# start with a storage id, never `/`, so the leading slash selects binds only).
sources=$(pct config "$vmid" | sed -n 's|^mp[0-9]*: \(/[^,]*\),.*mp=.*|\1|p')
[ -n "$sources" ] || exit 0

for src in $sources; do
    case "$src" in
        /mnt/nfs/*) ;;
        *) continue ;;
    esac

    if [ ! -r "$list" ]; then
        echo "$tag: $src is under /mnt/nfs/ but $list is missing — run the proxmox_host role" >&2
        exit 1
    fi

    mnt=""
    while IFS= read -r p; do
        case "$p" in ''|'#'*) continue ;; esac
        case "$src" in
            "$p"|"$p"/*) mnt="$p" ;;
        esac
    done < "$list"
    if [ -z "$mnt" ]; then
        echo "$tag: $src is under /mnt/nfs/ but no proxmox_nfs_mounts entry covers it" >&2
        exit 1
    fi

    unit=$(systemd-escape -p --suffix=mount "$mnt")
    deadline=$(( $(date +%s) + wait_s ))
    until systemctl start "$unit" >/dev/null 2>&1 && mountpoint -q "$mnt"; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "$tag: $unit ($mnt) is not mounted after ${wait_s}s — is the NFS server up? Refusing to start with an empty $src" >&2
            exit 1
        fi
        sleep 5
    done

    if [ ! -d "$src" ]; then
        echo "$tag: $mnt is mounted but $src is not a directory on the share — refusing to bind a path that does not exist" >&2
        exit 1
    fi
    echo "$tag: $src is live on $unit"
done

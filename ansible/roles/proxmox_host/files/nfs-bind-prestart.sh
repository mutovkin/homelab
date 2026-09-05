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
# to prevent. Likewise a failed `pct config` read is a refusal, not "no binds"
# (review of #254 measured the first version exiting 0 there).
#
# Every source is resolved with realpath and must be the path it claims to be:
# a symlink or `..` planted on the share by ANY writer (every Mapall'd client can
# write there) would otherwise make LXC bind an arbitrary HOST path into the CT —
# mount(MS_BIND) resolves symlinks in the source, and NFS symlinks resolve on
# the client, i.e. on this hypervisor.
#
# Paths are word-split, so mountpoints and bind sources must not contain spaces
# or glob characters (the role's asserts refuse them at declaration time).
set -eu
set -f  # no glob expansion while word-splitting $sources

vmid="$1"
phase="$2"
[ "$phase" = "pre-start" ] || exit 0

list=/etc/proxmox-nfs-mounts.list
wait_s="${NFS_BIND_WAIT:-180}"
tag="nfs-bind-prestart[CT $vmid]"

# Read and parse SEPARATELY: `a | sed` takes sed's exit status, so a failed
# `pct config` would look like "no bind mounts" and let the CT start ungated.
cfg=$(pct config "$vmid") || {
    echo "$tag: pct config $vmid failed (rc $?) — refusing to start ungated" >&2
    exit 1
}
# Bind-mount sources: `mpN: /abs/path,...,mp=/inside` (allocation-form volumes
# start with a storage id, never `/`, so the leading slash selects binds only).
sources=$(printf '%s\n' "$cfg" | sed -n 's|^mp[0-9]*: \(/[^,]*\),.*mp=.*|\1|p')
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

    # Longest matching mountpoint wins (a child dataset exported separately must
    # not be satisfied by its parent's mount, where it appears as an EMPTY dir).
    mnt=""
    while IFS= read -r p; do
        case "$p" in ''|'#'*) continue ;; esac
        case "$src" in
            "$p"|"$p"/*) [ ${#p} -gt ${#mnt} ] && mnt="$p" ;;
        esac
    done < "$list"
    if [ -z "$mnt" ]; then
        echo "$tag: $src is under /mnt/nfs/ but no proxmox_nfs_mounts entry covers it" >&2
        exit 1
    fi

    unit=$(systemd-escape -p --suffix=mount "$mnt")
    deadline=$(( $(date +%s) + wait_s ))
    start_err=""
    until start_err=$(systemctl start "$unit" 2>&1 >/dev/null) && mountpoint -q "$mnt"; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "$tag: $unit ($mnt) is not mounted after ${wait_s}s — refusing to start with an empty $src" >&2
            [ -n "$start_err" ] && echo "$tag: last systemctl start error: $start_err" >&2
            systemctl status "$unit" --no-pager -l 2>&1 | sed "s/^/$tag:   /" >&2 || true
            exit 1
        fi
        sleep 5
    done

    if [ -L "$src" ]; then
        echo "$tag: $src is a symlink on the share — refusing to bind through it" >&2
        exit 1
    fi
    real=$(realpath -e -- "$src") || {
        echo "$tag: $mnt is mounted but $src does not exist on the share — refusing to bind a path that does not exist" >&2
        exit 1
    }
    if [ "$real" != "$src" ]; then
        echo "$tag: $src resolves to $real, not to itself — refusing to bind a path that is not what it claims" >&2
        exit 1
    fi
    if [ ! -d "$src" ]; then
        echo "$tag: $src is not a directory on the share — refusing" >&2
        exit 1
    fi
    echo "$tag: $src is live on $unit"
done

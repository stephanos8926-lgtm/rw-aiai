#!/bin/bash
# vdrive-manager — systemd service that manages virtual drive lifecycle
# 
# Actions:
#   mount    — ensures vdrive image exists, formatted, and mounted (called @boot)
#   umount   — safely unmounts all vdrives (called @shutdown)
#   grow     — expand sparse image if usage exceeds threshold
#   status   — report mount health and usage
#   sync     — push vdrive image to remote target (if enabled)
#
# Config: /etc/vdrive/config.yml

set -euo pipefail

CONFIG="${VDRIBE_CONFIG:-/etc/vdrive/config.yml}"
LOGFILE="${VDRIBE_LOG:-/tmp/vdrive-manager.log}"

log() { 
    if [ -p /dev/stdin ] && [ ! -t 0 ]; then
        while IFS= read -r line; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$LOGFILE"
        done
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
    fi
}

# ─── Config helpers ──────────────────────────────────────────────────────────

read_config() {
    if ! command -v yq &>/dev/null; then
        # Fallback: parse with python3 (yq isn't always installed)
        python3 -c "
import yaml, sys, json
with open('$CONFIG') as f:
    print(json.dumps(yaml.safe_load(f)))
" 2>/dev/null || { log "FATAL: Cannot read $CONFIG (install yq or pyyaml)"; exit 1; }
    fi
}

get_vdrives() {
    read_config | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
for name, v in cfg.get('vdrives', {}).items():
    print(f\"{name}|{v.get('image_path')}|{v.get('mount_point')}|{v.get('size','20G')}|{v.get('filesystem','btrfs')}|{v.get('compression','zstd')}|{v.get('auto_grow',False)}\")
"
}

get_remote_config() {
    read_config | python3 -c "
import json, sys
cfg = json.load(sys.stdin).get('remote_backup', {})
print(f\"{cfg.get('enabled',False)}|{cfg.get('target_host')}|{cfg.get('target_user')}|{cfg.get('target_path')}|{cfg.get('method','rsync')}|{cfg.get('bandwidth_limit',0)}\")
"
}

# ─── Core actions ────────────────────────────────────────────────────────────

cmd_mount() {
    log "=== Mounting vdrives ==="
    get_vdrives | while IFS='|' read -r name image_path mount_point size fs compression auto_grow; do
        log "Processing vdrive: $name"
        
        # Create parent dirs
        mkdir -p "$(dirname "$image_path")" "$mount_point"
        
        # Create sparse image if missing
        if [ ! -f "$image_path" ]; then
            log "Creating $size sparse image: $image_path"
            dd if=/dev/zero of="$image_path" bs=1M count=1 seek="${size%G}G" status=none
            log "Formatting with $fs..."
            case "$fs" in
                btrfs) mkfs.btrfs -q -L "$name" "$image_path" ;;
                ext4)  mkfs.ext4 -q -L "$name" "$image_path" ;;
                *)     log "FATAL: Unknown filesystem $fs"; exit 1 ;;
            esac
            log "✅ $image_path created and formatted"
        fi
        
        # Check if already mounted
        if mountpoint -q "$mount_point"; then
            log "Already mounted at $mount_point"
            continue
        fi
        
        # Mount with compression if btrfs
        mount_opts="loop,discard"
        [ "$fs" = "btrfs" ] && [ -n "$compression" ] && mount_opts="loop,discard,compress=$compression"
        
        mount -o "$mount_opts" "$image_path" "$mount_point"
        log "✅ Mounted $image_path at $mount_point (opts=$mount_opts)"
        
        # Verify
        df -h "$mount_point" | tail -1 | log
    done
    log "=== All vdrives mounted ==="
}

cmd_umount() {
    log "=== Unmounting vdrives ==="
    get_vdrives | while IFS='|' read -r name image_path mount_point _; do
        if mountpoint -q "$mount_point"; then
            umount "$mount_point" && log "✅ Unmounted $mount_point" || log "WARN: Failed to unmount $mount_point"
        else
            log "Not mounted: $mount_point"
        fi
    done
    log "=== All vdrives unmounted ==="
}

cmd_grow() {
    log "=== Checking vdrive sizes ==="
    get_vdrives | while IFS='|' read -r name image_path mount_point size fs _ auto_grow; do
        [ "$auto_grow" != "True" ] && continue
        
        usage=$(df --output=pcent "$mount_point" | tail -1 | tr -d ' %')
        if [ "$usage" -gt 80 ]; then
            current_size=$(stat -c%s "$image_path")
            new_size=$(( current_size * 12 / 10 ))  # grow by 20%
            
            log "Usage ${usage}% > 80% — growing $image_path from ${size}..."
            
            cmd_umount
            truncate -s "$new_size" "$image_path"
            
            # For btrfs, need to resize the filesystem
            cmd_mount
            btrfs filesystem resize max "$mount_point"
            
            log "✅ $image_path grown"
        else
            log "Usage ${usage}% — within threshold"
        fi
    done
}

cmd_trim() {
    log "=== Trimming vdrive filesystems ==="
    get_vdrives | while IFS='|' read -r name image_path mount_point _ fs _ _; do
        if mountpoint -q "$mount_point"; then
            # fstrim tells the underlying file we don't need these blocks
            fstrim -v "$mount_point" 2>&1 | log
        fi
    done
}

cmd_status() {
    echo "=== vdrive-manager status ==="
    echo "Config: $CONFIG"
    echo ""
    get_vdrives | while IFS='|' read -r name image_path mount_point size fs compression _; do
        echo "--- $name ---"
        echo "  Image:  $image_path"
        echo "  Size:   $(stat -c%s "$image_path" 2>/dev/null | numfmt --to=iec 2>/dev/null || echo "$size")"
        echo "  Actual: $(du -h "$image_path" 2>/dev/null | cut -f1 || echo '?')"
        echo "  FS:     $fs"
        if mountpoint -q "$mount_point"; then
            echo "  Status: ✅ mounted at $mount_point"
            df -h "$mount_point" | tail -1 | awk '{print "  Usage:  " $5 " of " $2}'
        else
            echo "  Status: ❌ not mounted"
        fi
    done
    
    echo ""
    IFS='|' read -r enabled host user path method _ < <(get_remote_config)
    echo "Remote backup: $([ "$enabled" = "True" ] && echo "✅ enabled" || echo "❌ disabled")"
    [ "$enabled" = "True" ] && echo "  Target: $user@$host:$path"
}

cmd_sync() {
    IFS='|' read -r enabled host user path method bw_limit < <(get_remote_config)
    [ "$enabled" != "True" ] && { log "Remote backup disabled — skipping sync"; return 0; }
    
    log "=== Syncing vdrives to $user@$host:$path ==="
    
    bw_arg=""
    [ "$bw_limit" -gt 0 ] 2>/dev/null && bw_arg="--bwlimit=${bw_limit}"
    
    get_vdrives | while IFS='|' read -r name image_path mount_point _ _ _ _; do
        image_name=$(basename "$image_path")
        target="$user@$host:$path/$image_name"
        
        log "Syncing $image_path → $target"
        rsync -avz --sparse -e ssh "$image_path" "$target" 2>&1 | log
        log "✅ $image_name synced"
    done
}

# ─── CLI dispatch ────────────────────────────────────────────────────────────

case "${1:-mount}" in
    mount)   cmd_mount ;;
    umount)  cmd_umount ;;
    unmount) cmd_umount ;;  # common typo
    grow)    cmd_grow ;;
    trim)    cmd_trim ;;
    status)  cmd_status ;;
    sync)    cmd_sync ;;
    *)
        echo "Usage: $0 {mount|umount|grow|trim|status|sync}"
        exit 1
        ;;
esac

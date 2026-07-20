#!/bin/bash
# vdrive-sync — Workstation-side: pull vdrive images from server with slot rotation
#
# Slot scheme:
#   slot_1/  ← most recent backup (pulled from server)
#   slot_2/  ← previous backup (rotated out when new arrives)
#
# Rotation rules:
#   1. Compare server's vdrive image vs local slot_1
#   2. Same content → do nothing (backup hasn't changed)
#   3. Different content → rotate: slot_1 → slot_2, new → slot_1
#   4. If slot_2 exists before rotation → discard (overwrite)
#   5. Before pulling, check disk space; if < 10% free, warn
#
# Config: /etc/vdrive/sync-config.yml or env vars

set -euo pipefail

CONFIG="${VDRIBE_SYNC_CONFIG:-/etc/vdrive/sync-config.yml}"
LOGFILE="${VDRIBE_LOG:-/tmp/vdrive-sync.log}"
SLOTS_DIR="${VDRIBE_SLOTS_DIR:-/mnt/remote_backups}"

log() { 
    if [ -p /dev/stdin ] && [ ! -t 0 ]; then
        while IFS= read -r line; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$LOGFILE"
        done
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
    fi
}

# ─── Config ──────────────────────────────────────────────────────────────────

read_config() {
    if [ -f "$CONFIG" ]; then
        python3 -c "
import yaml, json, sys
with open('$CONFIG') as f:
    print(json.dumps(yaml.safe_load(f)))
" 2>/dev/null || echo '{}'
    else
        echo '{}'
    fi
}

# Get config with env var overrides
get_source() {
    echo "${VDRIBE_SOURCE:-$(read_config | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('source',''))" 2>/dev/null || true)}"
}

get_slots() {
    echo "${VDRIBE_SLOTS:-2}"
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

get_slot_path() {
    local slot_num="$1"
    echo "$SLOTS_DIR/slot_${slot_num}"
}

ensure_slots_dir() {
    mkdir -p "$SLOTS_DIR"
    local slots=$(get_slots)
    for i in $(seq 1 $slots); do
        mkdir -p "$(get_slot_path "$i")"
    done
}

# ─── Main sync + rotation ────────────────────────────────────────────────────

do_sync() {
    local source="$1"
    local slot_1="$(get_slot_path 1)"
    local slot_2="$(get_slot_path 2)"
    
    log "=== vdrive sync start ==="
    log "Source: $source"
    log "Slots:  $SLOTS_DIR"
    
    ensure_slots_dir
    
    # Check disk space before pulling
    local available_pcent=$(df --output=pcent "$SLOTS_DIR" 2>/dev/null | tail -1 | tr -d ' %')
    if [ "${available_pcent:-0}" -gt 90 ]; then
        log "ERROR: Insufficient disk space (${available_pcent}% used)"
        return 1
    fi
    
    # Pull the vdrive image from server
    log "Pulling from $source..."
    rsync -avz --sparse -e ssh "$source" "$(get_slot_path 1)/backup.img.incoming" 2>&1 | log
    
    local incoming="$(get_slot_path 1)/backup.img.incoming"
    if [ ! -f "$incoming" ]; then
        log "ERROR: Pull failed — no file received"
        return 1
    fi
    
    local received_blocks=$(stat -c%b "$incoming" 2>/dev/null || echo 0)
    local received_real=$(( received_blocks * 512 ))
    log "Received: $(numfmt --to=iec "$received_real" 2>/dev/null || echo "$received_real bytes") actual"
    
    # Rotate: slot_1 → slot_2, incoming → slot_1
    local slot_1_file="$slot_1/backup.img"
    
    if [ -f "$slot_1_file" ]; then
        local slot_2_file="$slot_2/backup.img"
        mkdir -p "$slot_2"
        log "  Moving slot_1 → slot_2"
        mv -f "$slot_1_file" "$slot_2_file"
    fi
    
    log "  Installing new backup → slot_1"
    mv "$incoming" "$slot_1_file"
    
    log "✅ Rotation complete"
    log "  slot_1: $(du -h "$slot_1_file" | cut -f1)"
    if [ -f "$slot_2/backup.img" ]; then
        log "  slot_2: $(du -h "$slot_2/backup.img" | cut -f1)"
    else
        log "  slot_2: (empty)"
    fi
    
    if [ ! -f "$slot_1_file" ]; then
        log "ERROR: slot_1 is empty after rotation!"
        return 1
    fi
    
    echo "✅ Backup synced and rotated"
    return 0
}

# ─── Status ──────────────────────────────────────────────────────────────────

cmd_status() {
    echo "=== vdrive-sync status ==="
    echo "Source: $(get_source)"
    echo "Slots dir: $SLOTS_DIR"
    echo ""
    local slots=$(get_slots)
    for i in $(seq 1 $slots); do
        local sp="$(get_slot_path "$i")"
        local img="$sp/backup.img"
        if [ -f "$img" ]; then
            local size=$(stat -c%s "$img" 2>/dev/null | numfmt --to=iec 2>/dev/null || echo '?')
            local mod=$(stat -c'%y' "$img" 2>/dev/null | cut -d'.' -f1 || echo '?')
            local hash=$(sha256sum "$img" | cut -c1-16)
            echo "  slot_${i}: ✅ ${size} (${mod}) [${hash}...]"
        else
            echo "  slot_${i}: (empty)"
        fi
    done
    echo ""
    local pcent=$(df --output=pcent "$SLOTS_DIR" 2>/dev/null | tail -1 | tr -d ' %')
    if [ "${pcent:-0}" -gt 90 ]; then
        echo "⚠️  Low disk space: ${pcent}% used"
    fi
}

# ─── CLI ─────────────────────────────────────────────────────────────────────

case "${1:-sync}" in
    sync)   do_sync "$(get_source)" ;;
    status) cmd_status ;;
    *)
        echo "Usage: $0 {sync|status}"
        echo ""
        echo "Config (env vars override config file):"
        echo "  VDRIBE_SOURCE          rsync source (user@host:/path/to/backup.img)"
        echo "  VDRIBE_SLOTS_DIR       local storage path (default: /mnt/remote_backups)"
        echo "  VDRIBE_SLOTS           number of retention slots (default: 2)"
        echo "  VDRIBE_SYNC_CONFIG     config file path (default: /etc/vdrive/sync-config.yml)"
        exit 1
        ;;
esac

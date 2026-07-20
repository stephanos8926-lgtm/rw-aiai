#!/bin/bash
# Prune old restic backups across all incremental VMs
# Usage: prune.sh [backup_root]

set -euo pipefail

BACKUP_ROOT="${1:-/mnt/vdrive/backups}"
RETENTION_DAILY="${2:-7}"
RETENTION_WEEKLY="${3:-4}"
RETENTION_MONTHLY="${4:-3}"

echo "🧹 Pruning restic repos under $BACKUP_ROOT..."

for repo_dir in "$BACKUP_ROOT"/*/; do
    repo="${repo_dir%/}"
    if [ -f "$repo/config" ]; then
        echo "  Pruning $repo..."
        restic -r "$repo" forget \
            --keep-daily "$RETENTION_DAILY" \
            --keep-weekly "$RETENTION_WEEKLY" \
            --keep-monthly "$RETENTION_MONTHLY" \
            --prune
    fi
done

echo "✅ Pruning complete"

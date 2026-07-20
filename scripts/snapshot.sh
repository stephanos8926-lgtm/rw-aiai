#!/bin/bash
# Quick Incus VM snapshot — for dev VM or ad-hoc use
# Usage: snapshot.sh [vm_name] [retention_days]

set -euo pipefail

VM="${1:-dev}"
RETENTION_DAYS="${2:-14}"
SNAPSHOT_NAME="manual-$(date +%Y%m%d-%H%M%S)"

echo "📸 Snapshotting VM: $VM → $SNAPSHOT_NAME"
incus snapshot "$VM" "$SNAPSHOT_NAME"

echo "🧹 Pruning snapshots older than ${RETENTION_DAYS}d..."
incus list-snapshots "$VM" --format=json | python3 -c "
import json, sys, datetime
snaps = json.load(sys.stdin)
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=$RETENTION_DAYS)
for s in snaps:
    created = s.get('created_at', '')
    if created:
        dt = datetime.datetime.fromisoformat(created)
        if dt < cutoff:
            name = s['name']
            print(f'  Pruning {name} (created {created})')
            import subprocess
            subprocess.run(['incus', 'delete-snapshot', '$VM/$name'], check=True)
"

echo "✅ Done — snapshots for $VM:" 
incus list-snapshots "$VM" | head -5

#!/usr/bin/env bash
# Cloud backup of NRL runtime files to Google Drive via rclone.
# Usage: ./cloud_backup.sh [--dry-run]
# Designed to run from systemd timer (daily at 02:30 UTC).
# See: https://github.com/bobcheong/NBAMonitor/issues/394

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="gdrive:openclaw-backups"
LOG_FILE="$SCRIPT_DIR/logs/cloud_backup.log"

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
fi

RCLONE_FLAGS=(
    --transfers 4
    --checkers 8
    --contimeout 30s
    --timeout 120s
    --retries 3
    --log-level INFO
    --stats-one-line
    --exclude "runtime_backups/**"
    --exclude ".git/**"
    --exclude ".venv/**"
    --exclude "__pycache__/**"
    --exclude "*.tmp"
    --exclude "*.tmp.*"
    --exclude "*.pyc"
    --exclude "*.png"
    --exclude "docs/superpowers/**"
    --exclude ".claude/**"
    --exclude ".github/**"
)

LOG_MAX_BYTES=5242880  # 5MB
LOG_KEEP=3

log() {
    local msg="[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1"
    echo "$msg"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$msg" >> "$LOG_FILE"
}

# Rotate log if over threshold (#250)
if [[ -f "$LOG_FILE" ]]; then
    _size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( _size > LOG_MAX_BYTES )); then
        for (( i=LOG_KEEP; i>1; i-- )); do
            [[ -f "$LOG_FILE.$((i-1))" ]] && mv "$LOG_FILE.$((i-1))" "$LOG_FILE.$i"
        done
        mv "$LOG_FILE" "$LOG_FILE.1"
    fi
fi

log "=== NRL cloud backup started ==="
log "START NRL: $SCRIPT_DIR -> $REMOTE/nrl-monitor ${DRY_RUN:+(dry run)}"

if rclone sync "$SCRIPT_DIR" "$REMOTE/nrl-monitor" "${RCLONE_FLAGS[@]}" $DRY_RUN 2>&1 | while read -r line; do
    log "  NRL: $line"
done; then
    log "=== NRL cloud backup completed successfully ==="
else
    log "=== NRL cloud backup completed with errors (non-fatal) ==="
fi

#!/usr/bin/env bash
# Add the nightly 02:30 backup to this user's crontab (idempotent; leaves other entries alone).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINE="30 2 * * * $ROOT/scripts/backup.sh >> $ROOT/backups/cron.log 2>&1"
mkdir -p "$ROOT/backups"
if crontab -l 2>/dev/null | grep -Fq "$ROOT/scripts/backup.sh"; then
  echo "already installed:"; crontab -l | grep -F "$ROOT/scripts/backup.sh"
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "installed: $LINE"
fi

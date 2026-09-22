#!/usr/bin/env bash
# Restore studyvault data from a backup snapshot into an EMPTY data directory.
#   scripts/restore.sh                 restore the newest snapshot in ~/studyvault/backups
#   scripts/restore.sh 2026-10-05_023000   restore that snapshot (name or full path)
# The app must be stopped first (docker compose down). If data/ has anything in it, move it aside first:
#   mv data data.old-$(date +%F)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${STUDYVAULT_DATA_DIR:-$ROOT/data}"
DEST="${BACKUP_LOCAL:-$ROOT/backups}"
CONTAINER="${STUDYVAULT_CONTAINER:-studyvault}"

pick="${1:-latest}"
if [[ "$pick" == "latest" ]]; then
  SNAP="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??_*' ! -name '*.partial' | sort | tail -1)"
elif [[ -d "$pick" ]]; then SNAP="$pick"
else SNAP="$DEST/$pick"; fi
[[ -n "$SNAP" && -f "$SNAP/studyvault.db" ]] || { echo "No snapshot found (looked for '$pick' in $DEST)"; exit 1; }

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "The $CONTAINER container is running. Stop it first: cd $ROOT && docker compose down"; exit 1
fi
if [[ -d "$DATA" ]] && [[ -n "$(ls -A "$DATA" 2>/dev/null)" ]]; then
  echo "$DATA is not empty. Move it aside first:  mv '$DATA' '$DATA.old-$(date +%F)'"; exit 1
fi

echo "Restoring from $SNAP"
( cd "$SNAP" && sha256sum -c --quiet SHA256SUMS ) || { echo "Checksum mismatch in $SNAP; try an older snapshot"; exit 1; }
mkdir -p "$DATA"
cp "$SNAP/studyvault.db" "$DATA/studyvault.db"
tar -xzf "$SNAP/notes.tar.gz" -C "$DATA"
tar -xzf "$SNAP/attachments.tar.gz" -C "$DATA"

python3 - "$DATA/studyvault.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity check failed"
print("restored:", ", ".join(f"{t} {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
      for t in ("courses", "competencies", "cards", "reviews", "questions", "quiz_attempts", "sessions")))
PY
echo "notes files: $(find "$DATA/notes" -name '*.md' | wc -l) · notes commits: $(git -C "$DATA/notes" rev-list --count HEAD 2>/dev/null || echo 0)"
echo "Done. Start the app: cd $ROOT && docker compose up -d"

#!/usr/bin/env bash
# Nightly studyvault backup. Runs on the Pi host (cron, 02:30), safe while the app is running.
#   1. SQLite online backup (Python's sqlite3.backup; no sqlite3 CLI on this Pi) + integrity check
#   2. git-commit the notes repo, then archive it (history included)
#   3. archive attachments
#   4. keep the newest 14 snapshots in $BACKUP_LOCAL (default ~/studyvault/backups)
#   5. copy the new snapshot to $BACKUP_TARGET (from .env, host:path or a mounted dir) and verify it there
# Exit code is non-zero if anything failed; the last result is in backups/LAST_RESULT.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${STUDYVAULT_DATA_DIR:-$ROOT/data}"
DEST="${BACKUP_LOCAL:-$ROOT/backups}"
KEEP="${BACKUP_KEEP:-14}"
TARGET="${BACKUP_TARGET-$(grep -E '^BACKUP_TARGET=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | sed 's/[[:space:]]*#.*//' || true)}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
SNAP="$DEST/$STAMP"
LOG="$DEST/backup.log"

mkdir -p "$DEST"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
fail() { log "FAILED: $*"; echo "FAILED $STAMP: $*" > "$DEST/LAST_RESULT"; rm -rf "$SNAP.partial"; exit 1; }
trap 'fail "line $LINENO"' ERR

[[ -f "$DATA/studyvault.db" ]] || fail "no database at $DATA/studyvault.db"
mkdir -p "$SNAP.partial"
log "backup $STAMP starting (data: $DATA)"

# 1. database
python3 - "$DATA/studyvault.db" "$SNAP.partial/studyvault.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
src.close()
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("courses", "competencies", "cards", "reviews", "questions", "quiz_attempts", "sessions")}
dst.close()
if ok != "ok":
    sys.exit(f"integrity_check: {ok}")
print(" ".join(f"{k}={v}" for k, v in counts.items()))
PY

# 2. notes (commit whatever the app hasn't committed yet, then archive incl. .git)
if [[ -d "$DATA/notes/.git" ]]; then
  git -C "$DATA/notes" -c user.name=studyvault-backup -c user.email=studyvault@localhost add -A
  git -C "$DATA/notes" -c user.name=studyvault-backup -c user.email=studyvault@localhost commit -q -m "backup: $STAMP" >/dev/null || true
fi
tar -czf "$SNAP.partial/notes.tar.gz" -C "$DATA" notes
# 3. attachments
mkdir -p "$DATA/attachments"
tar -czf "$SNAP.partial/attachments.tar.gz" -C "$DATA" attachments

( cd "$SNAP.partial" && sha256sum studyvault.db notes.tar.gz attachments.tar.gz > SHA256SUMS )
mv "$SNAP.partial" "$SNAP"
log "snapshot $SNAP ($(du -sh "$SNAP" | cut -f1))"

# 4. prune (snapshot dirs are named by timestamp, so name order = age order)
mapfile -t snaps < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??_*' ! -name '*.partial' | sort)
if (( ${#snaps[@]} > KEEP )); then
  for old in "${snaps[@]:0:${#snaps[@]}-KEEP}"; do rm -rf "$old"; log "pruned $(basename "$old")"; done
fi

# 5. off-device copy: push this snapshot over ssh (tar, so the target needs no rsync) and verify it there.
#    Push-only: nothing on the target is ever deleted, so a damaged local backups/ can't wipe the off-device copies.
if [[ -n "$TARGET" ]]; then
  trap - ERR; set +e +o pipefail
  host="${TARGET%%:*}"; dir="${TARGET#*:}"
  if [[ "$host" == "$TARGET" ]]; then  # a mounted drive, not host:path
    mkdir -p "$TARGET" && cp -a "$SNAP" "$TARGET/" && ( cd "$TARGET/$STAMP" && sha256sum -c --quiet SHA256SUMS ); rc=$?
  else
    set -o pipefail; tar -C "$DEST" -cf - "$STAMP" | ssh -o BatchMode=yes -o ConnectTimeout=20 "$host" \
      "mkdir -p '$dir' && tar -C '$dir' -xf - && cd '$dir/$STAMP' && sha256sum -c --quiet SHA256SUMS"; rc=$?
  fi
  if (( rc == 0 )); then
    log "copied to $TARGET and verified"
    echo "OK $STAMP -> $TARGET (verified)" > "$DEST/LAST_RESULT"
  else
    log "FAILED: off-device copy to $TARGET (local snapshot $STAMP is fine)"
    echo "FAILED $STAMP: off-device copy to $TARGET failed; local snapshot OK" > "$DEST/LAST_RESULT"
    exit 1
  fi
else
  log "WARNING: BACKUP_TARGET is empty; snapshot is only on this SD card"
  echo "OK $STAMP (local only: set BACKUP_TARGET in .env)" > "$DEST/LAST_RESULT"
fi

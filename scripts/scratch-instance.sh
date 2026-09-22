#!/usr/bin/env bash
# Throwaway studyvault on 127.0.0.1:8421 with its own temp data, for manual checks that
# would otherwise change real data (e.g. marking a course passed makes it "attempted" forever).
#   scripts/scratch-instance.sh up     start (fresh data every time)
#   scripts/scratch-instance.sh down   stop and delete its data
set -euo pipefail
DIR=/tmp/studyvault-scratch
case "${1:-up}" in
  up)
    docker rm -f sv-scratch >/dev/null 2>&1 || true
    rm -rf "$DIR" && mkdir -p "$DIR"
    docker run -d --name sv-scratch --platform linux/arm/v7 -p 127.0.0.1:8421:8000 -m 250m \
      -e STUDYVAULT_PASSWORD=scratch -e STUDYVAULT_SECRET=scratch -e TZ=America/New_York \
      -e STUDYVAULT_DEBUG="${STUDYVAULT_DEBUG:-}" -v "$DIR":/data studyvault:latest >/dev/null
    for _ in $(seq 20); do curl -sf http://127.0.0.1:8421/health >/dev/null && break; sleep 1; done
    echo "scratch instance up: http://127.0.0.1:8421 (password: scratch)"
    ;;
  down)
    docker rm -f sv-scratch >/dev/null 2>&1 || true
    rm -rf "$DIR"
    echo "scratch instance removed"
    ;;
esac

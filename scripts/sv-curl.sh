#!/usr/bin/env bash
# Logged-in curl against the scratch instance (runs on the Pi). Usage: scripts/sv-curl.sh <path> [curl args...]
set -euo pipefail
JAR=/tmp/studyvault-scratch.cookies
BASE=http://127.0.0.1:8421
if [[ "$(curl -s -o /dev/null -w "%{http_code}" -b "$JAR" "$BASE/terms")" != 200 ]]; then
  curl -s -c "$JAR" -o /dev/null -d password=scratch "$BASE/login"
fi
path="$1"; shift
curl -s -b "$JAR" -c "$JAR" "$BASE$path" "$@"

#!/usr/bin/env bash
# Ship the repo from the build PC to the Pi, rebuild, run tests. Never touches data/ or .env on the Pi.
#   scripts/deploy.sh            deploy + build + test + (re)start
#   scripts/deploy.sh --no-test  skip pytest
set -euo pipefail
# Target Pi: STUDYVAULT_HOST=user@pi-address, from the environment or a git-ignored .deploy.env file.
[[ -z "${STUDYVAULT_HOST:-}" && -f "$(dirname "$0")/../.deploy.env" ]] && source "$(dirname "$0")/../.deploy.env"
HOST="${STUDYVAULT_HOST:?set STUDYVAULT_HOST=user@pi-address (or put it in .deploy.env)}"
cd "$(dirname "$0")/.."

tar --exclude=./data --exclude=./.env --exclude=./.deploy.env --exclude=./.git --exclude='__pycache__' --exclude='.pytest_cache' -czf - . \
  | ssh "$HOST" 'set -e; mkdir -p ~/studyvault/data; cd ~/studyvault; rm -rf app tests seed scripts; tar -xzf - ; chmod +x scripts/*.sh'

# compose v5's builder needs Docker API 1.52; the Pi's daemon is 20.10 (API 1.41). Build with plain docker.
ssh "$HOST" 'cd ~/studyvault && docker build -q --platform linux/arm/v7 -t studyvault:latest .'
if [[ "${1:-}" != "--no-test" ]]; then
  ssh "$HOST" 'docker run --rm --platform linux/arm/v7 studyvault:latest python -m pytest -q -p no:cacheprovider'
fi
ssh "$HOST" 'cd ~/studyvault && docker compose up -d --no-build && sleep 4 && docker compose ps'

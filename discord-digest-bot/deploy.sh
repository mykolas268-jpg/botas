#!/usr/bin/env bash
# One-command deploy/update on a Linux server (Ubuntu/Debian tested path).
#   First time:  git clone <repo> && cd <repo>/discord-digest-bot && ./deploy.sh
#   Update:      git pull && ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "==> Docker not found, installing (needs sudo)..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
    echo "==> Docker installed. Log out and back in (or run 'newgrp docker'), then re-run ./deploy.sh"
    exit 0
fi

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "==> Created .env from .env.example. Fill in DISCORD_TOKEN, DISCORD_CHANNEL_ID (and NEWSAPI_KEY),"
    echo "    e.g. 'nano .env', then re-run ./deploy.sh"
    exit 1
fi

echo "==> Building image..."
docker compose build

echo "==> Running preflight check..."
if ! docker compose run --rm --no-deps digest-bot python check.py; then
    echo "==> Preflight check failed - bot NOT started. Fix the ❌ items above and re-run."
    exit 1
fi

echo "==> Starting bot..."
docker compose up -d
sleep 5
docker compose logs --tail 20 digest-bot
echo "==> Done. Follow logs with: docker compose logs -f"

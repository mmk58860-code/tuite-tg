#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

docker compose up -d --build

echo "Tuite TG 已启动："
echo "http://服务器IP:${WEB_PORT:-8000}"

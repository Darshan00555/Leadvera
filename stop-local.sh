#!/usr/bin/env bash
# Stop the local portal. Data is kept (Docker volumes). To wipe everything: add -v to the command below.
cd "$(dirname "$0")"
docker compose -f docker-compose.local.yml --env-file .env.local down
echo "Stopped. Your data is kept. Start again with ./start-local.sh"

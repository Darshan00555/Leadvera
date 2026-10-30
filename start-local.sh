#!/usr/bin/env bash
# Start Leadvera on this computer (Mac / Linux) with Docker.
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose -f docker-compose.local.yml --env-file .env.local)

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop: https://www.docker.com/products/docker-desktop/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Open Docker Desktop, wait until it says 'running', then re-run." >&2
  exit 1
fi

secret() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

if [ ! -f .env.local ]; then
  echo "First-time setup - creating .env.local"
  read -r -p "Admin email (your login): " ADMIN_EMAIL
  while true; do
    read -r -s -p "Admin password (12+ chars, upper + lower case + a digit, no \$ sign): " ADMIN_PASSWORD; echo
    if [ ${#ADMIN_PASSWORD} -ge 12 ] && [[ "$ADMIN_PASSWORD" =~ [A-Z] ]] && [[ "$ADMIN_PASSWORD" =~ [a-z] ]] \
       && [[ "$ADMIN_PASSWORD" =~ [0-9] ]] && [[ "$ADMIN_PASSWORD" != *'$'* ]]; then break; fi
    echo "  Password does not meet the rules, try again."
  done
  read -r -p "Anthropic API key (leave EMPTY if you will use your Claude Pro/Max plan - press Enter): " ANTHROPIC_API_KEY
  read -r -p "Port for the portal [8000]: " PORT; PORT=${PORT:-8000}
  umask 077
  cat > .env.local <<EOF
SECRET_KEY=$(secret 64)
POSTGRES_PASSWORD=$(secret 32)
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
PORT=$PORT
EOF
  echo "Saved .env.local (keep it private - it holds your secrets)."
fi

PORT=$(grep -E '^PORT=' .env.local | cut -d= -f2); PORT=${PORT:-8000}
echo "Starting the database..."
"${COMPOSE[@]}" up -d --wait db
# An existing pgdata volume keeps the password it was created with; make it match .env.local.
# The SQL goes through stdin so the password never appears on a command line.
PG_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' .env.local | cut -d= -f2-)
printf "ALTER USER afportal WITH PASSWORD '%s';\n" "${PG_PASSWORD//\'/\'\'}" | \
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -q -U afportal -d afportal
echo "Building and starting containers (first run downloads ~300 MB, takes a few minutes)..."
"${COMPOSE[@]}" up -d --build

echo -n "Waiting for the portal"
for _ in $(seq 1 90); do
  if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo; echo "Portal is running: http://localhost:$PORT"
    (command -v open >/dev/null && open "http://localhost:$PORT") || \
      (command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT" >/dev/null 2>&1) || true
    echo "Logs:  docker compose -f docker-compose.local.yml --env-file .env.local logs -f worker"
    echo "Stop:  ./stop-local.sh"
    echo "Use your Claude Pro/Max plan for research:  ./connect-claude.sh  (then paste the token in Settings)"
    exit 0
  fi
  echo -n "."; sleep 2
done
echo; echo "The portal did not answer in 3 minutes. Recent logs:" >&2
"${COMPOSE[@]}" logs --tail 40 web worker >&2
exit 1

#!/usr/bin/env bash
# Updates EVE Distance Meter on a Linux host: pulls from GitHub, rebuilds and restarts the container, checks health.
#   scripts/update.sh             git pull --ff-only, docker compose up -d --build, wait until healthy
#   scripts/update.sh --no-pull   only rebuild and restart
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env is missing: cp .env.example .env and fill it in (docs/wiki/04-Deployment.md)"; exit 1; }
docker version --format '{{.Server.Os}}' >/dev/null 2>&1 \
  || { echo "Docker is not running, or this user may not use it (sudo usermod -aG docker \$USER, then log in again)"; exit 1; }

mkdir -p data
if [ "$(stat -c %u data)" != "10001" ]; then  # the container runs as uid 10001 and must write to ./data
  echo "== data/ must belong to uid 10001 (the container user)"
  if [ "$(id -u)" = "0" ]; then chown -R 10001:10001 data; else sudo chown -R 10001:10001 data; fi
fi

if [ "${1:-}" != "--no-pull" ]; then
  echo "== git pull --ff-only"
  git pull --ff-only
fi

# Docker refuses a CPU limit above the host's CPU count: cap EVEDM_CPUS (environment, else .env, else 2) at nproc
host_cpus="$(nproc)"
want="${EVEDM_CPUS:-$(sed -n 's/^EVEDM_CPUS=//p' .env | tail -n 1 | tr -d '\r" ')}"
want="${want:-2}"
if awk -v w="$want" -v h="$host_cpus" 'BEGIN { exit !(w + 0 > h + 0) }'; then
  echo "== EVEDM_CPUS=$want is more than this host's $host_cpus CPU(s): using $host_cpus"
  want="$host_cpus"
fi
export EVEDM_CPUS="$want"

echo "== docker compose up -d --build"
docker compose up -d --build

echo "== waiting for the container to become healthy"
status=""
for _ in $(seq 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' evedm 2>/dev/null || true)"
  echo "  ${status:-starting}"
  [ "$status" = "healthy" ] && break
  sleep 3
done
if [ "$status" != "healthy" ]; then
  docker compose logs --tail 40 evedm
  echo "The container is '${status:-not running}' after 3 minutes: see the log above."
  exit 1
fi

port="$(docker compose port evedm 8080 | awk -F: '{print $NF}')"
echo "== health"
curl -fsS "http://127.0.0.1:${port}/api/health" | head -c 400
echo
echo "Done: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${port}"

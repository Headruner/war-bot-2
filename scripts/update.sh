#!/usr/bin/env bash
# Updates EVE Distance Meter on a Linux host from the image GitHub Actions published: nothing is built on the host.
#   scripts/update.sh               git pull the compose file, config and scripts, docker compose pull, up -d, wait until healthy
#   scripts/update.sh --no-git      skip the git step: only pull the image and restart  (--no-pull is the old name)
#   scripts/update.sh --build       build the image from the checked-out source instead of pulling it
#   scripts/update.sh --force-sync  main was force-pushed: reset the checkout to origin/main (old HEAD kept as a backup branch)
# Image: EVEDM_IMAGE (environment or .env), default ghcr.io/headruner/war-bot-2:latest. Pin a version with
#   EVEDM_IMAGE=ghcr.io/headruner/war-bot-2:v0.8.0   (roll back the same way). A private package needs `docker login ghcr.io`.
set -euo pipefail
cd "$(dirname "$0")/.."

use_git=1 build=0 force=0
for arg in "$@"; do
  case "$arg" in
    --no-git|--no-pull) use_git=0 ;;
    --build) build=1 ;;
    --force-sync) force=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see scripts/update.sh --help)"; exit 2 ;;
  esac
done

[ -f .env ] || { echo ".env is missing: cp .env.example .env and fill it in (docs/wiki/04-Deployment.md)"; exit 1; }
docker version --format '{{.Server.Os}}' >/dev/null 2>&1 \
  || { echo "Docker is not running, or this user may not use it (sudo usermod -aG docker \$USER, then log in again)"; exit 1; }

mkdir -p data
if [ "$(stat -c %u data)" != "10001" ]; then  # the container runs as uid 10001 and must write to ./data
  echo "== data/ must belong to uid 10001 (the container user)"
  if [ "$(id -u)" = "0" ]; then chown -R 10001:10001 data; else sudo chown -R 10001:10001 data; fi
fi

if [ "$use_git" = 1 ]; then
  # only the compose file, config/wars.yaml and the scripts come from git now; the program itself is in the image
  branch="${EVEDM_BRANCH:-main}"
  echo "== git fetch origin $branch"
  [ "$(git rev-parse --abbrev-ref HEAD)" = "$branch" ] || git checkout "$branch"
  git fetch origin "$branch"
  if git merge-base --is-ancestor HEAD "origin/$branch"; then
    git merge --ff-only "origin/$branch"
  elif [ "$force" = 1 ]; then
    backup="backup/pre-sync-$(date +%Y%m%d-%H%M%S)"
    echo "== origin/$branch was rewritten: resetting to it (old HEAD kept as $backup)"
    git branch "$backup"
    git reset --hard "origin/$branch"
  else
    echo "origin/$branch was force-pushed (its history was rewritten), so a fast-forward is not possible."
    echo "Run scripts/update.sh --force-sync to reset this host to origin/$branch (the old HEAD stays as a backup branch)."
    exit 1
  fi
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

if [ "$build" = 1 ]; then
  echo "== docker compose up -d --build   (building from this checkout)"
  docker compose up -d --build
else
  image="${EVEDM_IMAGE:-$(sed -n 's/^EVEDM_IMAGE=//p' .env | tail -n 1 | tr -d '\r" ')}"
  export EVEDM_IMAGE="${image:-ghcr.io/headruner/war-bot-2:latest}"
  echo "== docker compose pull   ($EVEDM_IMAGE)"
  if ! docker compose pull evedm; then
    echo "Could not pull $EVEDM_IMAGE."
    echo "Private package: docker login ghcr.io -u <github user>  (a token with read:packages), or make the package public"
    echo "(GitHub > Packages > Package settings). No image yet: check that the 'ci' workflow finished on main."
    exit 1
  fi
  echo "== docker compose up -d"
  docker compose up -d --no-build
  docker image prune -f >/dev/null 2>&1 || true  # the image this one replaced
fi

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

revision="$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' evedm 2>/dev/null | cut -c1-7 || true)"
echo "== running $(docker inspect -f '{{.Config.Image}}' evedm 2>/dev/null)${revision:+ (revision $revision)}"
port="$(docker compose port evedm 8080 | awk -F: '{print $NF}')"
echo "== health"
curl -fsS "http://127.0.0.1:${port}/api/health" | head -c 400
echo
echo "Done: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${port}"

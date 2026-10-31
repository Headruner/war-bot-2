#!/usr/bin/env bash
# Smoke test for the image: 2 CPU / 1 GB limits, host folder mounted as /app/data, demo mode,
# non-root user, SQLite in WAL mode, war watcher config, data kept across a restart.
#   scripts/docker-smoke.sh [image]        (default evedm:0.6.0)
set -euo pipefail
IMAGE="${1:-evedm:0.6.0}"
NAME="evedm-smoke-$$"
DATA="$(mktemp -d)"
chmod 0777 "$DATA"   # the container runs as uid 10001 (non-root)
PORT="${PORT:-18080}"
LOG_DIR="$(cd "$(dirname "$0")/.." && pwd)/logs/launches"
mkdir -p "$LOG_DIR"
cleanup() { docker logs "$NAME" > "$LOG_DIR/docker-smoke-$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1 || true; docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

run() {
  docker run -d --name "$NAME" --memory 1g --cpus 2 -p "$PORT:8080" -v "$DATA:/app/data" \
    -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=smoke-test-secret "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null)" = "healthy" ] && return 0
    sleep 1
  done
  echo "container did not become healthy"; docker logs "$NAME"; return 1
}

run
curl -fsS "http://127.0.0.1:$PORT/api/health" | tee /dev/stderr | grep -q '"status":"ok"'
curl -fsS "http://127.0.0.1:$PORT/api/rank?target=Tama" | grep -q '"recommended":true'
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/"
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/wars"   # war watcher on (config/wars.yaml in the image), demo wars
curl -fsS "http://127.0.0.1:$PORT/api/wars" | grep -q '"war_id":990001' || { echo "Wars page API has no demo wars"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/api/wars/stats" | grep -q '"active":8' || { echo "war statistics missing"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/api/wars/structures" | grep -q '"matched":10' || { echo "high-sec structure list missing"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/api/structures" | grep -q '"matched":7' || { echo "Structures page API missing"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/api/alerts?refresh=0" | grep -q '"core_drop":true' || { echo "Alerts API missing"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/api/health" | grep -q '"journal_mode":"wal"' || { echo "SQLite is not in WAL mode"; exit 1; }
docker exec "$NAME" evedm check | grep -q '"ok": true' || { echo "evedm check failed"; exit 1; }
docker stats --no-stream --format 'memory {{.MemUsage}}  cpu {{.CPUPerc}}' "$NAME"
docker exec "$NAME" sh -c 'grep -E "^Uid|VmRSS|VmHWM" /proc/1/status'
docker exec "$NAME" sh -c 'test "$(awk "/^Uid/{print \$2}" /proc/1/status)" = 10001' || { echo "server is not running as uid 10001"; exit 1; }
docker stop -t 10 "$NAME" >/dev/null && docker rm "$NAME" >/dev/null

# restart on the same mounted folder: data and launch history must survive
run
LAUNCHES=$(docker exec "$NAME" evedm launches | wc -l)
[ "$LAUNCHES" -ge 2 ] || { echo "expected >= 2 launches, got $LAUNCHES"; exit 1; }
ls -l "$DATA" "$DATA/logs"
echo "smoke test passed: $LAUNCHES launches recorded in $DATA/evedm.sqlite3"

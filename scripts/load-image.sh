#!/usr/bin/env bash
# Joins image/evedm-image-0.3.0-amd64.tar.gz.part-* (if split) and loads it into Docker.
set -euo pipefail
cd "$(dirname "$0")/../image"
FILE=evedm-image-0.3.0-amd64.tar.gz
[ -f "$FILE" ] || cat "$FILE".part-* > "$FILE"
sha256sum -c "$FILE.sha256"
docker load -i "$FILE"

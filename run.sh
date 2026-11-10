#!/bin/sh
# Build and run Return to Mordor in Docker.
#
# Usage:
#   ./run.sh            (build + run on port 8010)
#
# The SQLite world state (accounts, characters, positions) lives in the
# named volume return-to-mordor-data, so containers keep everyone exactly
# where they left off across updates.
set -e
cd "$(dirname "$0")"

IMAGE=return-to-mordor

docker build -t "$IMAGE" .
docker rm -f return-to-mordor 2>/dev/null || true
docker run -d --name return-to-mordor \
  -p 8010:8000 \
  -v return-to-mordor-data:/data \
  --restart unless-stopped \
  "$IMAGE"

echo "Game: http://127.0.0.1:8010  (from other machines: http://<this-host>:8010)"

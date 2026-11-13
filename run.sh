#!/bin/sh
# Build and run Quest for Earth in Docker.
#
# Usage:
#   ./run.sh            (build + run on port 8010)
#
# The SQLite world state (accounts, characters, positions) lives in the
# named volume quest-for-earth-data, so containers keep everyone exactly
# where they left off across updates. NOTE: the volume schema changed with
# the Quest for Earth conversion — old return-to-mordor volumes are not
# compatible; start fresh.
set -e
cd "$(dirname "$0")"

IMAGE=quest-for-earth

docker build -t "$IMAGE" .
docker rm -f quest-for-earth 2>/dev/null || true
docker run -d --name quest-for-earth \
  -p 8010:8000 \
  -v quest-for-earth-data:/data \
  --restart unless-stopped \
  "$IMAGE"

echo "Game: http://127.0.0.1:8010  (from other machines: http://<this-host>:8010)"

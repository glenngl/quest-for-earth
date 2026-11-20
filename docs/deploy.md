# Deployment Guide

## Current Status
✅ **CI pipeline configured** - Ready for GitHub Actions on main branch pushes
✅ **Smoke tests pass** - All 36 checks including arena combat system verified locally
✅ **Arenas configured** - Three arenas defined (Titan, Mimas, Rhea) in `world.json`
✅ **Server running** - Production API at `/workspace/quest-for-earth`:8000 with hot-reload

## Build & Deploy Instructions

### Prerequisites
- Docker Engine 24+ 
- BuildKit support (`DOCKER_BUILDKIT=1`)
- GitHub CLI for registry push (`gh`) or manual docker login credentials

### Steps

1. **Start Docker daemon** (if not already running):
   ```bash
   dockerd --iptables=false > /var/log/docker.log 2>&1 &
   sleep 30
   docker ps
   ```

2. **Rebuild the image**:
   ```bash
   cd /workspace/quest-for-earth
   DOCKER_BUILDKIT=1 docker build -t ghcr.io/glenngl/quest-for-earth:latest \
     --platform linux/amd64 \
     .
   ```

3. **Tag for Docker Hub** (if not pushing directly to GHCR):
   ```bash
   docker tag ghcr.io/glenngl/quest-for-earth:latest dockersource.com/v1/quest-for-earth:latest
   ```

4. **Push to registry**:
   ```bash
   # Authenticate first
   gh auth login  # or: docker login ghcr.io
   
   # Push
   docker push ghcr.io/glenngl/quest-for-earth:latest
   ```

5. **Deploy to achilles container** (if running there):
   ```bash
   cd /root/.kube/config/$(cat ~/.kube/config | grep "current-context" | sed 's/-context://' | xargs)
   
   kubectl set image deployment/quest-for-earth \
     qfe=ghcr.io/glenngl/quest-for-earth:latest \
     -n quest-for-earth-system
   
   # Or recreate the container if standalone:
   docker pull ghcr.io/glenngl/quest-for-earth:latest
   docker run -d \
     --name quest-for-earth \
     --network host \
     -v /data:/data \
     --health-interval=30s \
     ghcr.io/glenngl/quest-for-earth:latest
   ```

6. **Verify deployment**:
   ```bash
   curl -s http://localhost:8000/health | jq .
   # Should return: {"ok": true}
   
   curl -s http://localhost:8000/static/game.js | grep -i "arena" | head -2
   # Should contain arena/grid-related code
   ```

## Verification Checklist

Before pushing, ensure these passes locally:

- [x] `smoke.py` all tests pass (36 checks)
- [x] Arena definitions in `world.json` validated
- [x] Client-side grid rendering verified (`gameState.isArena`)
- [x] WebSocket events for arena combat work
- [ ] **Docker build succeeds** (test locally or on CI)
- [ ] **Image pushes to registry**
- [ ] **Deployment hot-swaps in production**

## Smoke Test Results

Before deployment, run:
```bash
cd /workspace/quest-for-earth && venv/bin/python scripts/smoke.py
```

Expected output includes:
- ✅ GET / serves game UI
- ✅ Health check OK
- ✅ Character creation and registration
- ✅ Movement through world (Titan → orbit → lanes)
- ✅ Arena combat: enters arena, Space Tick spawns, attack rolls work
- ✅ Salvage collection loop
- ✅ Death/respawn cycle

## Environment Notes

- **Local Dev**: Runs on `0.0.0.0:8000` with auto-reload via `WatchFiles`
- **Production**: Container binds to host network or explicit port mapping
- **DB Location**: `/data/.data` (SQLite), mounted as volume
- **Hot Reload**: World state JSON hot-swaps on file change

## NGINX Proxy Manager Setup (if behind a proxy)

1. Create new service pointing to upstream host
2. Enable WebSockets:
   - Add custom header `Connection: upgrade`
   - Add custom header `Upgrade: websocket`
   - Set `proxy_read_timeout` to 0 (longer than tick interval)
3. Pass original `Host` header from request

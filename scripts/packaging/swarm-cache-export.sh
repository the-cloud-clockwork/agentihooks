#!/usr/bin/env bash
set -euo pipefail

docker buildx build --output type=cacheonly \
  --cache-to type=gha,version=2,mode=max,scope=swarm-smoke,compression=zstd,force-compression=true .

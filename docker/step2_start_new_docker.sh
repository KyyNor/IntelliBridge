#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 重启 intellibridge 服务
echo "Restarting intellibridge service..."
docker-compose stop intellibridge
docker-compose rm -f intellibridge
docker-compose up -d intellibridge

# 清理1天以前的dangling镜像（none标签）
echo "Cleaning up old dangling images (none tag, older than 24h)..."
docker image prune -f --filter "dangling=true" --filter "until=24h"

echo "Done!"

docker-compose logs -f intellibridge
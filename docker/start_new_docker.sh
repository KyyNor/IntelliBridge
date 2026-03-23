#!/bin/bash

DATA_PATH=/home/bdapp/intellibridge_data

# 停止并删除已有容器
echo "Stopping existing container..."
docker stop intellibridge 2>/dev/null || true
docker rm intellibridge 2>/dev/null || true

# 启动新容器
echo "Starting new container..."
docker run -d \
    --name intellibridge \
    --restart unless-stopped \
    -p 49000:49000 \
    -p 49001:49001 \
    -v $DATA_PATH/cache:/app/cache \
    -v $DATA_PATH/logs:/app/logs \
    -v $DATA_PATH/config:/app/config \
    intellibridge:latest

# 清理1天以前的dangling镜像（none标签）
echo "Cleaning up old dangling images (older than 24h)..."
docker image prune -af --filter "until=24h"

echo "Done!"

docker logs -f intellibridge
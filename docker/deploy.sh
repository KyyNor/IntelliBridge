#!/bin/bash
# IntelliBridge 一键部署脚本
# 自动完成：git pull → 构建镜像 → 启动容器

set -e

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}IntelliBridge 一键部署脚本${NC}"
echo -e "${BLUE}========================================${NC}"

# Step 1: Git Pull
echo ""
echo -e "${GREEN}[Step 1/3] Git Pull${NC}"
echo -e "${YELLOW}--------------------------------------${NC}"
cd "${PROJECT_ROOT}"
git fetch origin
git pull origin master
echo -e "${GREEN}Git pull 完成${NC}"

# Step 2: 构建镜像
echo ""
echo -e "${GREEN}[Step 2/3] 构建 Docker 镜像${NC}"
echo -e "${YELLOW}--------------------------------------${NC}"
bash "${SCRIPT_DIR}/step1_build.sh"

# Step 3: 启动容器
echo ""
echo -e "${GREEN}[Step 3/3] 启动 Docker 容器${NC}"
echo -e "${YELLOW}--------------------------------------${NC}"
bash "${SCRIPT_DIR}/step2_start_new_docker.sh"
#!/bin/bash
# IntelliBridge Docker 构建脚本
# 支持内网部署

set -e

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}==================================${NC}"
echo -e "${GREEN}IntelliBridge Docker 构建脚本${NC}"
echo -e "${GREEN}==================================${NC}"

# 切换到 docker 目录
cd "${SCRIPT_DIR}"

# 检查是否存在 .build.env
if [ ! -f ".build.env" ]; then
    echo -e "${YELLOW}未找到 .build.env 文件，从模板创建...${NC}"
    cp build.env.example .build.env
    echo -e "${YELLOW}请编辑 .build.env 文件配置镜像源后重新运行${NC}"
    exit 1
fi

# 加载环境变量
export $(cat .build.env | grep -v '^#' | xargs)

# 设置默认值
PYTHON_PIP_INDEX_URL=${PYTHON_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmmirror.com}
NODE_VERSION=${NODE_VERSION:-24.1.0}
NODEJS_DISTFILE=${NODEJS_DISTFILE:-node-v24.1.0-linux-x64.tar.xz}

echo -e "${GREEN}配置信息：${NC}"
echo "  Python 镜像源: ${PYTHON_PIP_INDEX_URL}"
echo "  NPM 镜像源: ${NPM_REGISTRY}"
echo "  Node.js 版本: ${NODE_VERSION}"

# 检查 Node.js 压缩包
if [ ! -f "build/${NODEJS_DISTFILE}" ]; then
    echo -e "${RED}错误: 未找到 Node.js 压缩包 build/${NODEJS_DISTFILE}${NC}"
    echo -e "${YELLOW}请先下载 Node.js 二进制包：${NC}"
    echo "  mkdir -p docker/build"
    echo "  curl -L -o docker/build/${NODEJS_DISTFILE} \\"
    echo "    https://mirrors.aliyun.com/nodejs-release/v${NODE_VERSION}/${NODEJS_DISTFILE}"
    exit 1
fi

echo -e "${GREEN}找到 Node.js 压缩包: build/${NODEJS_DISTFILE}${NC}"

# 解压 Node.js
echo -e "${GREEN}解压 Node.js 到 build/ 目录...${NC}"
NODE_EXTRACTED_DIR="build/node-${NODE_VERSION}-linux-x64"

# 如果已解压过，先删除
if [ -d "${NODE_EXTRACTED_DIR}" ]; then
    echo -e "${YELLOW}删除旧的解压目录...${NC}"
    rm -rf "${NODE_EXTRACTED_DIR}"
fi

# 解压
mkdir -p "${NODE_EXTRACTED_DIR}"
tar -xf "build/${NODEJS_DISTFILE}" -C build/

echo -e "${GREEN}Node.js 解压完成${NC}"

# 构建镜像
IMAGE_TAG=${IMAGE_TAG:-intellibridge:latest}
echo -e "${GREEN}开始构建 Docker 镜像: ${IMAGE_TAG}${NC}"

# 切换到项目根目录构建
cd "${PROJECT_ROOT}"
docker build \
  -f docker/Dockerfile \
  --build-arg PYTHON_PIP_INDEX_URL=${PYTHON_PIP_INDEX_URL} \
  --build-arg NPM_REGISTRY=${NPM_REGISTRY} \
  --build-arg NODE_VERSION=${NODE_VERSION} \
  -t ${IMAGE_TAG} .

if [ $? -eq 0 ]; then
    echo -e "${GREEN}==================================${NC}"
    echo -e "${GREEN}构建成功！${NC}"
    echo -e "${GREEN}镜像: ${IMAGE_TAG}${NC}"
    echo -e "${GREEN}==================================${NC}"

    # 显示镜像大小
    IMAGE_SIZE=$(docker inspect ${IMAGE_TAG} --format='{{.Size}}' | awk '{printf "%.1f MB", $1/1024/1024}')
    echo -e "${GREEN}镜像大小: ${IMAGE_SIZE}${NC}"

    # 验证镜像
    echo -e "${GREEN}验证镜像...${NC}"
    docker run --rm ${IMAGE_TAG} sh -c "node --version && npm --version && agent-browser --version"

    # 清理解压的 Node.js 目录
    echo -e "${YELLOW}清理临时文件...${NC}"
    cd "${SCRIPT_DIR}"
    rm -rf "${NODE_EXTRACTED_DIR}"
    echo -e "${GREEN}完成！${NC}"
else
    echo -e "${RED}构建失败！${NC}"
    exit 1
fi

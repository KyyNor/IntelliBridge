#!/bin/bash
# Python 离线包下载脚本
# 用于下载 requirements.txt 中的所有依赖包到 build/python-packages/

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
echo -e "${GREEN}Python 离线包下载脚本${NC}"
echo -e "${GREEN}==================================${NC}"

# 切换到 docker 目录
cd "${SCRIPT_DIR}"

# 配置
PACKAGES_DIR="build/python-packages"
REQUIREMENTS_FILE="${PROJECT_ROOT}/requirements.txt"
PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
DOCKER_IMAGE=${DOCKER_IMAGE:-intellibridge:latest}

echo -e "${GREEN}配置信息：${NC}"
echo "  PyPI 镜像源: ${PIP_INDEX_URL}"
echo "  目标目录: ${PACKAGES_DIR}"
echo "  Docker 镜像: ${DOCKER_IMAGE}"

# 检查 requirements.txt 是否存在
if [ ! -f "${REQUIREMENTS_FILE}" ]; then
    echo -e "${RED}错误: 未找到 requirements.txt 文件${NC}"
    echo -e "${YELLOW}请确保在项目根目录下运行此脚本${NC}"
    exit 1
fi

# 检查 Docker 镜像是否存在
if ! docker image inspect ${DOCKER_IMAGE} &> /dev/null; then
    echo -e "${RED}错误: Docker 镜像 ${DOCKER_IMAGE} 不存在${NC}"
    echo -e "${YELLOW}请先构建镜像: ./build.sh${NC}"
    exit 1
fi

# 创建目标目录
echo -e "${GREEN}创建目标目录...${NC}"
mkdir -p ${PACKAGES_DIR}

# 清理旧的包
echo -e "${YELLOW}清理旧的离线包...${NC}"
rm -rf ${PACKAGES_DIR}/*

# 使用容器下载（确保兼容性）
echo -e "${GREEN}开始下载 Python 离线包...${NC}"
echo -e "${YELLOW}使用 Docker 容器下载（确保与目标环境兼容）${NC}"

docker run --rm -v $(pwd)/${PACKAGES_DIR}:/packages -v ${PROJECT_ROOT}:/app ${DOCKER_IMAGE} \
  bash -c "pip download -r /app/requirements.txt -d /packages -i ${PIP_INDEX_URL}"

if [ $? -eq 0 ]; then
    # 统计信息
    FILE_COUNT=$(ls ${PACKAGES_DIR} | wc -l)
    DIR_SIZE=$(du -sh ${PACKAGES_DIR} | cut -f1)

    echo -e "${GREEN}==================================${NC}"
    echo -e "${GREEN}下载完成！${NC}"
    echo -e "${GREEN}==================================${NC}"
    echo -e "${GREEN}文件数量: ${FILE_COUNT}${NC}"
    echo -e "${GREEN}总大小: ${DIR_SIZE}${NC}"
    echo -e "${GREEN}保存位置: ${PACKAGES_DIR}${NC}"
    echo -e ""
    echo -e "${YELLOW}提示: 可以使用以下命令打包传输到内网${NC}"
    echo -e "  tar -czf python-packages.tar.gz -C docker/build python-packages/"
else
    echo -e "${RED}下载失败！${NC}"
    exit 1
fi

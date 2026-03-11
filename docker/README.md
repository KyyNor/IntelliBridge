# Docker 部署

本目录包含 IntelliBridge 的 Docker 部署相关文件。

## 📁 目录结构

```
docker/
├── Dockerfile              # 标准 Dockerfile（在线安装）
├── Dockerfile.offline      # 离线 Dockerfile（支持离线包）
├── build.sh                # 构建脚本
├── download-wheels.sh      # Python 离线包下载脚本
├── build.env.example       # 构建环境变量模板
├── .dockerignore           # Docker 构建忽略文件
├── README.md               # 本文件
└── build/                  # 构建产物目录
    ├── node-v*.tar.xz                 # Node.js 压缩包（需下载）
    ├── node-v*-linux-x64/             # Node.js 解压目录（自动生成）
    └── python-packages/               # Python 离线包（可选）
```

## 🚀 快速开始

### 1. 配置构建环境

```bash
cd docker

# 复制配置模板
cp build.env.example .build.env

# 编辑配置（可选，默认使用国内镜像源）
vim .build.env
```

### 2. 下载 Node.js

```bash
# 下载 Node.js 24.1.0
mkdir -p build
curl -L -o build/node-v24.1.0-linux-x64.tar.xz \
  https://mirrors.aliyun.com/nodejs-release/v24.1.0/node-v24.1.0-linux-x64.tar.xz
```

### 3. 构建镜像

```bash
# 使用构建脚本（推荐）
./build.sh

# 或者直接使用 docker build
cd ..
docker build -f docker/Dockerfile -t intellibridge:latest .
```

## 📦 离线部署

### 内网环境部署

#### 方法一：使用离线 Dockerfile

```bash
# 1. 下载 Python 离线包（在有外网的机器上）
./download-wheels.sh

# 2. 打包所有部署文件
tar -czf intellibridge-offline.tar.gz \
  docker/ \
  config/ \
  tools/ \
  utils/ \
  main.py \
  requirements.txt

# 3. 传输到内网并解压
# 4. 使用离线 Dockerfile 构建
docker build -f docker/Dockerfile.offline -t intellibridge:latest .
```

#### 方法二：预构建镜像

```bash
# 在有外网的机器上构建镜像
./build.sh

# 导出镜像
docker save intellibridge:latest | gzip > intellibridge.tar.gz

# 传输到内网并导入
gunzip -c intellibridge.tar.gz | docker load
```

## 🔧 脚本说明

### build.sh

自动化构建脚本，功能：
- 检查配置文件
- 解压 Node.js 压缩包
- 构建 Docker 镜像
- 验证镜像
- 清理临时文件

```bash
./build.sh

# 自定义镜像标签
IMAGE_TAG=intellibridge:v1.0.0 ./build.sh
```

### download-wheels.sh

下载 Python 依赖的 wheel 文件到 `build/python-packages/`。

```bash
./download-wheels.sh

# 使用自定义 PyPI 源
PIP_INDEX_URL=http://内网-pypi/simple ./download-wheels.sh
```

## 📝 配置文件

### build.env

构建环境变量配置：

```bash
# Python 镜像源
PYTHON_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# NPM 镜像源
NPM_REGISTRY=https://registry.npmmirror.com

# Node.js 版本
NODE_VERSION=24.1.0
NODEJS_DISTFILE=node-v24.1.0-linux-x64.tar.xz
```

## 📊 镜像信息

- **基础镜像**: python:3.12-slim
- **Node.js 版本**: 24.1.0
- **agent-browser 版本**: 0.17.1
- **镜像大小**: 约 316 MB

## 🔗 相关文档

- [内网部署指南](../docs/内网部署指南.md)
- [Python离线包使用指南](../docs/Python离线包使用指南.md)

## ❓ 常见问题

### Q: 修改了 requirements.txt 后怎么办？

A: 如果使用离线部署，需要重新下载 Python 包：
```bash
./download-wheels.sh
```

### Q: 如何更换 Node.js 版本？

A: 修改 `build.env` 中的 `NODE_VERSION` 和 `NODEJS_DISTFILE`，然后下载对应的压缩包。

### Q: build/ 目录下的文件会被提交到 Git 吗？

A: 不会。`.gitignore` 已配置忽略 `docker/build/` 下的构建产物（tar.xz、解压目录、python-packages）。

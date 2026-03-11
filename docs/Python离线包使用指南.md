# Python 离线包使用指南

本指南介绍如何使用预下载的 Python 离线包在内网环境中安装 IntelliBridge 依赖。

## 环境信息

| 项目 | 版本 |
|------|------|
| 操作系统 | Debian GNU/Linux 13 (trixie) |
| Python 版本 | 3.12.13 |
| pip 版本 | 25.1.1 |
| 平台架构 | linux_x86_64 |

## 离线包信息

- **目录**: `build/python-packages/`
- **文件数量**: 82 个 wheel 文件
- **总大小**: 约 22 MB
- **包含内容**: requirements.txt 中的所有依赖包

## 使用方法

### 方法一：修改 Dockerfile 使用离线包

在内网环境中，修改 Dockerfile 使用本地离线包：

```dockerfile
FROM python:3.12-slim

# ... (前面部分保持不变)

# 复制离线 Python 包
COPY build/python-packages/ /tmp/packages/

# 使用离线包安装依赖
RUN pip install --no-cache-dir --no-index --find-links=/tmp/packages -r requirements.txt

# ... (后面部分保持不变)
```

### 方法二：使用修改后的构建脚本

1. 将 `build/python-packages/` 目录传输到内网服务器
2. 确保 build.sh 脚本中支持离线安装模式
3. 运行构建脚本

### 方法三：在现有 Python 环境中安装

如果需要在已有 Python 3.12 环境中安装：

```bash
# 进入离线包目录
cd build/python-packages/

# 安装所有包（需要 Python 3.12）
pip install --no-index --find-links=. -r ../../requirements.txt
```

### 方法四：搭建内网 PyPI 镜像

使用离线包搭建内网 PyPI 镜像服务器：

```bash
# 使用 pypiserver 搭建简单的 PyPI 镜像
pip install pypiserver
pypiserver -p 8080 build/python-packages/

# 在其他机器上使用
pip install -r requirements.txt -i http://内网服务器:8080/simple
```

## 下载离线包

如果需要重新下载或更新离线包：

### 在有外网的机器上

```bash
# 方法 1: 使用容器下载（推荐，确保兼容性）
docker run --rm -v $(pwd)/build/python-packages:/packages intellibridge:latest \
  bash -c "pip download -r /app/requirements.txt -d /packages -i https://pypi.tuna.tsinghua.edu.cn/simple"

# 方法 2: 使用 Python 3.12 直接下载
mkdir -p build/python-packages
pip download -r requirements.txt -d build/python-packages \
  --platform linux_x86_64 \
  --only-binary=:all: \
  --python-version 3.12 \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 更新 requirements.txt 后

当 `requirements.txt` 发生变化时，需要重新下载离线包：

```bash
# 清理旧的离线包
rm -rf build/python-packages/*

# 重新下载
./build/download-wheels.sh  # 如果创建了下载脚本
```

## 验证离线包

在传输到内网前，验证离线包完整性：

```bash
# 检查文件数量
ls build/python-packages/ | wc -l
# 预期输出: 82

# 检查总大小
du -sh build/python-packages/
# 预期输出: 约 22M

# 验证可以正常安装（在测试环境中）
docker run --rm -v $(pwd)/build/python-packages:/packages \
  python:3.12-slim \
  bash -c "pip install --no-index --find-links=/packages fastapi fastmcp && python -c 'import fastapi, fastmcp; print(\"OK\")'"
```

## 传输到内网

### 打包

```bash
# 只打包 Python 离线包
tar -czf python-packages.tar.gz -C build python-packages/

# 打包所有部署文件（包括 Python 离线包）
tar -czf intellibridge-deploy-full.tar.gz \
  build.sh \
  Dockerfile \
  requirements.txt \
  .build.env \
  build/node-v24.1.0-linux-x64.tar.xz \
  build/python-packages/ \
  config/ \
  tools/ \
  utils/ \
  main.py
```

### 在内网解压

```bash
tar -xzf intellibridge-deploy-full.tar.gz
cd intellibridge
```

## 常见问题

### Q: 离线包是否包含所有依赖？

A: 是的，`pip download` 会递归下载所有依赖包（包括子依赖），共 82 个文件。

### Q: 离线包是否适用于其他 Python 版本？

A: 这些 wheel 文件是针对 Python 3.12 + Linux x86_64 编译的。如果使用其他版本，需要重新下载。

### Q: 如何处理某些包没有预编译 wheel 的情况？

A: 如果内网环境有编译工具（gcc 等），可以在下载时不限制 `--only-binary`，这样会下载源码包。但这通常需要安装额外的系统依赖。

### Q: 离线包是否会过期？

A: 只要 requirements.txt 中的版本号不变，离线包就可以一直使用。如果更新了依赖版本，需要重新下载。

### Q: 可以在没有 Docker 的环境中使用吗？

A: 可以，只要有 Python 3.12 环境，就可以直接使用 `pip install --no-index --find-links=build/python-packages/ -r requirements.txt` 安装。

## 依赖包列表

主要依赖包（共 82 个）：

- **Web 框架**: fastapi, uvicorn, starlette
- **MCP 框架**: fastmcp, mcp
- **数据库**: pymysql, pyhive (hive_pure_sasl)
- **数据验证**: pydantic, pydantic-core
- **缓存**: diskcache
- **SQL 解析**: sqlglot
- **HTTP 客户端**: httpx, httpcore
- **工具库**: loguru, click, pyyaml, python-dotenv
- **其他依赖**: cryptography, authlib, rich 等

完整列表见 `build/python-packages/` 目录。

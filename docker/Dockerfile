FROM python:3.12-slim

# 引入构建参数（支持内网镜像源）
ARG PYTHON_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG NODE_VERSION=24.1.0

WORKDIR /app

# 复制并解压 Node.js（从 docker/build/ 目录）
# build.sh 脚本会先解压 Node.js 压缩包到 docker/build/node-v{VERSION}-linux-x64/
# 这样可以避免在容器内安装 xz-utils（内网可能无 apt 源）
COPY docker/build/node-v${NODE_VERSION}-linux-x64/ /usr/local/

# 验证 Node.js 版本
RUN npm --version && node --version

# 设置 npm 镜像源
RUN npm config set registry ${NPM_REGISTRY}

# 安装 agent-browser
RUN npm install -g agent-browser && \
    agent-browser --version

# 复制 requirements
COPY requirements.txt .

# 使用指定的镜像源安装 Python 依赖
RUN pip install --no-cache-dir -i ${PYTHON_PIP_INDEX_URL} -r requirements.txt

# 复制应用代码
COPY . .

# 暴露端口
EXPOSE 49000

# 运行应用
# CMD sh -c "agent-browser --session startup --session-name startup --cdp http://browserless:3000 open http://125.1.129.76:8080/fr/ && python main.py"
CMD sh -c "python main.py"

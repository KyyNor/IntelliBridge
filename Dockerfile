FROM python:3.12-slim

# 引入构建参数（支持内网镜像源）
ARG PYTHON_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

# 安装 xz 工具并复制 Node.js 二进制包
# 内网部署时：将下载好的 Node.js tar.gz 包放在 build/ 目录
# 下载地址：https://mirrors.aliyun.com/nodejs-release/v24.1.0/node-v24.1.0-linux-x64.tar.xz
RUN apt-get update && apt-get install -y xz-utils && rm -rf /var/lib/apt/lists/*
COPY build/node-v24.1.0-linux-x64.tar.xz /tmp/
RUN tar -xf /tmp/node-*.tar.xz -C /usr/local --strip-components=1 && \
    rm /tmp/node-*.tar.xz && \
    node --version && npm --version

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
EXPOSE 49000 49001

# 运行应用
CMD ["python", "main.py"]

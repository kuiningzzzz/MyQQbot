# syntax=docker/dockerfile:1.4

# 使用官方 Python 运行时作为父镜像
FROM node:24-bookworm-slim AS dsh-runtime
WORKDIR /build
COPY dsh/package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.11-slim
# 服务器位于大陆网络：debian 官方源极慢（曾出现 30+ 分钟卡在依赖安装），统一切换到清华镜像。
# 注意：下面的 playwright install --with-deps 内部同样会调用 apt，因此换源必须在它之前完成。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|; s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|' /etc/apt/sources.list.d/debian.sources
COPY --from=dsh-runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends bash git ripgrep libstdc++6 fonts-noto-cjk tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra poppler-utils && rm -rf /var/lib/apt/lists/*
COPY --from=dsh-runtime /build/node_modules /app/dsh/node_modules

# 设置工作目录
WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT}
ENV PIP_RETRIES=${PIP_RETRIES}
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# 复制依赖文件
COPY requirements.txt .

# 服务器无 GPU：先装 CPU 版 torch，避免 pip 解析到默认 PyPI 的 CUDA 全家桶
# （torch+cudnn+cublas 等约 3~4GB，纯属浪费镜像体积与构建时间）。
ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --progress-bar off --index-url ${PYTORCH_CPU_INDEX_URL} \
        "torch==2.14.0" \
    || pip install --progress-bar off --index-url ${PYTORCH_CPU_INDEX_URL} torch

# 安装其余依赖（torch 已满足 sentence-transformers 的版本要求，不会再升级为 CUDA 版）
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --progress-bar off -r requirements.txt

# Install login browser at build time; runtime requests never run apt or download executables.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
# 大陆网络下官方 CDN 实测仅约 24KB/s（Chromium 约 195MB，需 2 小时以上），改用 npmmirror 镜像
# （实测约 12MB/s）。如需回到官方源，删除本行即可。
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN python -m playwright install --with-deps chromium

# 复制项目文件
COPY . .

# 暴露机器人和管理 WebUI 端口
EXPOSE 8081
EXPOSE 10010

# 启动应用
CMD ["python", "-m", "src.main"]

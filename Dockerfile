# syntax=docker/dockerfile:1.4

# 使用官方 Python 运行时作为父镜像
FROM python:3.11-slim

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

# 复制项目文件
COPY . .

# 暴露机器人和管理 WebUI 端口
EXPOSE 8081
EXPOSE 10010

# 启动应用
CMD ["python", "-m", "src.main"]

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

# 安装依赖
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --progress-bar off -r requirements.txt

# 复制项目文件
COPY . .

# 暴露机器人和管理 WebUI 端口
EXPOSE 8081
EXPOSE 10010

# 启动应用
CMD ["python", "-m", "src.main"]

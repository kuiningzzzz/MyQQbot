# 使用官方 Python 运行时作为父镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装依赖
RUN pip install -r requirements.txt

# 复制项目文件
COPY . .

# 暴露机器人和管理 WebUI 端口
EXPOSE 8081
EXPOSE 10010

# 启动应用
CMD ["python", "-m", "src.main"]

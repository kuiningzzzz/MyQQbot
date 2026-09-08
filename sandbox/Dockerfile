FROM python:3.11-slim
# 服务器位于大陆网络，debian 官方源/pypi 官方源过慢，统一切换到清华镜像。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|; s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|' /etc/apt/sources.list.d/debian.sources \
    && cat /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-eng \
    poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    'Pillow>=10,<13' 'openpyxl>=3.1,<4' \
    'python-docx>=1.1,<2' 'reportlab>=4,<5' 'pypdf>=5,<7'
COPY sandbox /opt/worker
WORKDIR /opt/worker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-I", "/opt/worker/server.py"]

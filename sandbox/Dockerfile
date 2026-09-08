FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-eng \
    poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'Pillow>=10,<13' 'openpyxl>=3.1,<4' \
    'python-docx>=1.1,<2' 'reportlab>=4,<5' 'pypdf>=5,<7'
COPY sandbox /opt/worker
WORKDIR /opt/worker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-I", "/opt/worker/server.py"]

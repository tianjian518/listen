FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME_PATH=/usr/bin/chromium \
    PORT=8000

# 系统依赖：ffmpeg（音频转码）+ chromium（扫码登录）+ playwright 运行库
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        chromium \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        fonts-liberation curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# 运行时数据（配置 + 临时下载）落地到 /data，便于持久化挂载
RUN mkdir -p /data/downloads && chmod 777 /data/downloads
ENV MUSIC_PANEL_CONFIG=/data/config.json \
    MUSIC_PANEL_DOWNLOADS=/data/downloads

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

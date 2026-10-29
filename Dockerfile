FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# 构建时的软件源，默认官方。国内网络慢可加：
#   docker build --build-arg APT_MIRROR=mirrors.aliyun.com \
#                --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/ -t listen .
ARG APT_MIRROR=""
ARG PIP_INDEX=""

# 系统依赖只有 ffmpeg（音频转码）。扫码登录走纯 HTTP，不需要浏览器。
# 顺带删掉两样用不上的：ffmpeg 硬依赖的 libplacebo 会拖进一整套 Mesa 图形栈
# （LLVM 124M + gallium 41M + z3 27M），以及 doc / man / 非中文 locale。
# 删完实测 wma → m4a 转码正常。
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null \
        || sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    rm -f /usr/lib/x86_64-linux-gnu/libLLVM.so.* \
          /usr/lib/x86_64-linux-gnu/libgallium*.so* \
          /usr/lib/x86_64-linux-gnu/libz3.so.*; \
    rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/locale/* /usr/share/info/*; \
    mkdir -p /usr/share/locale/zh_CN

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX" ]; then \
        pip install --index-url "$PIP_INDEX" -r requirements.txt; \
    else \
        pip install -r requirements.txt; \
    fi \
    && pip uninstall -y pip setuptools wheel 2>/dev/null || true

COPY . .

# 运行时数据（配置 + 临时下载）落地到 /data，便于持久化挂载
RUN mkdir -p /data/downloads && chmod 777 /data/downloads
ENV MUSIC_PANEL_CONFIG=/data/config.json \
    MUSIC_PANEL_DOWNLOADS=/data/downloads

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

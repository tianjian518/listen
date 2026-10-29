#!/usr/bin/env bash
# 启动面板
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
VENV="${VENV:-/opt/venv}"

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "找不到虚拟环境 $VENV，先跑 install.sh"
  exit 1
fi

exec "$VENV/bin/uvicorn" app:app --host 0.0.0.0 --port "$PORT" --workers 1

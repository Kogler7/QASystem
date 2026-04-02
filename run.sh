#!/bin/bash
cd "$(dirname "$0")/backend"

# 启动后端服务
echo "启动后端服务..."
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
  echo "使用环境.venv/bin/activate"
else
  echo "未找到虚拟环境，使用系统 Python"
fi
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 后台启动后端服务
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &

# lsof -i :8000
# kill -9 
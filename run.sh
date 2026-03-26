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
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 启动前端服务（可选，取消下面注释以同时启动前端）
# echo "启动前端服务..."
# npm run dev

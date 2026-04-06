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

# 如果程序已启动，先停止后重启
PIDS=$(lsof -ti :8000)
if [ -n "$PIDS" ]; then
  echo "检测到 8000 端口已有进程: $PIDS，正在停止..."
  kill $PIDS
  sleep 1

  REMAINING_PIDS=$(lsof -ti :8000)
  if [ -n "$REMAINING_PIDS" ]; then
    echo "仍有进程未退出，强制停止: $REMAINING_PIDS"
    kill -9 $REMAINING_PIDS
  fi
fi

# 后台启动后端服务
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &
echo "后端服务已启动，PID: $!"
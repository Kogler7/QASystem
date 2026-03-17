#!/bin/bash
cd "$(dirname "$0")/frontend"

# 启动前端服务
echo "启动前端服务..."
if [ ! -d node_modules ]; then
  echo "未找到 node_modules，正在安装依赖..."
  npm install
fi
npm run dev

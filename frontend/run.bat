@echo off
cd /d "%~dp0"

:: 启动后端服务
echo 启动后端服务...
if exist backend\venv\Scripts\activate.bat (
  call backend\venv\Scripts\activate.bat
) else (
  echo 未找到虚拟环境，使用系统 Python
)
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

@REM :: 启动前端服务
@REM echo 启动前端服务...
@REM start "前端服务" npm run dev

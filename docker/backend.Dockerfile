# 轨道交通知识服务系统 - 后端镜像
# 必须在仓库根目录构建：docker compose build backend
# 包含 backend/app 与 algorithm/，以支持组件管理中的进程内算法调用。

FROM python:3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独 COPY，便于缓存（安装耗时较长，镜像体积较大属预期）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app ./backend/app
COPY algorithm ./algorithm

WORKDIR /app/backend
ENV PYTHONPATH=/app

RUN mkdir -p data/documents data/preproc_output data/preproc_upload

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

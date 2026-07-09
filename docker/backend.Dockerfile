# 轨道交通知识服务系统 - 后端镜像
# 必须在仓库根目录构建：docker compose build backend
# 包含 backend/app 与 algorithm/，以支持组件管理中的进程内算法调用。

FROM node:20-bullseye-slim AS opencode-runtime

RUN npm install -g opencode-ai

FROM python:3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libreoffice-core \
    libreoffice-writer \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独 COPY，便于缓存（安装耗时较长，镜像体积较大属预期）
COPY backend/requirements.txt backend/requirements.docker.txt ./backend/
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.extra-index-url "https://mirrors.aliyun.com/pypi/simple/ https://pypi.doubanio.com/simple/" && \
    pip config set global.trusted-host "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com pypi.doubanio.com"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r backend/requirements.docker.txt

# Basic tools for in-container inspection and small emergency edits.
# Keep this in a separate layer so tool changes do not reinstall Python deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    vim-tiny \
    less \
    procps \
    iproute2 \
    iputils-ping \
    dnsutils \
    netcat-openbsd \
    lsof \
    jq \
    ripgrep \
    tree \
    && rm -rf /var/lib/apt/lists/*

COPY --from=opencode-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-runtime /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY backend/app ./backend/app
COPY algorithm ./algorithm
COPY agentcode ./agentcode

RUN pip install --no-cache-dir -e '/app/agentcode[fastapi]'

WORKDIR /app/backend
ENV PYTHONPATH=/app:/app/agentcode

RUN mkdir -p data/documents data/preproc_output data/preproc_upload

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

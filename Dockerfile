# QASystem backend runtime image. Build from the repository root.

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
    libreoffice-impress \
    libreoffice-writer \
    fonts-dejavu-core \
    fonts-noto-cjk \
    curl \
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

RUN dpkg-query -W libreoffice-core libreoffice-writer libreoffice-impress fonts-noto-cjk && \
    libreoffice --headless --version

WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.extra-index-url "https://mirrors.aliyun.com/pypi/simple/ https://pypi.doubanio.com/simple/" && \
    pip config set global.trusted-host "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com pypi.doubanio.com" && \
    pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r backend/requirements.txt && \
    python -c "import docling, mineru, PIL; from docling.datamodel.base_models import InputFormat; assert hasattr(InputFormat, 'PPTX') and hasattr(InputFormat, 'IMAGE')"

COPY agent /opt/agent
RUN pip install --no-cache-dir --no-deps /opt/agent

COPY --from=opencode-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-runtime /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app/backend
ENV PYTHONPATH=/app:/app/agent \
    LIBREOFFICE_PATH=/usr/bin/soffice

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

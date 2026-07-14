# QASystem backend and Agent runtime image. Build from the repository root.

ARG NODE_IMAGE=node:20-bullseye-slim
ARG PYTHON_IMAGE=python:3.12-bookworm
FROM ${NODE_IMAGE} AS opencode-runtime

ARG OPENCODE_VERSION=1.17.15
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && \
    test "$(opencode --version)" = "${OPENCODE_VERSION}"

FROM ${PYTHON_IMAGE}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libmagic1 \
    libreoffice-core \
    libreoffice-impress \
    libreoffice-writer \
    fonts-dejavu-core \
    fonts-noto-cjk \
    ffmpeg \
    poppler-utils \
    curl \
    bash \
    ca-certificates \
    git \
    openssh-client \
    unzip \
    file \
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
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the heavy Python layer before copying application code so normal
# source changes continue to use Docker's dependency cache.
COPY backend/requirements.txt /tmp/qasystem/requirements.txt
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.extra-index-url "https://mirrors.aliyun.com/pypi/simple/ https://pypi.doubanio.com/simple/" && \
    pip config set global.trusted-host "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com pypi.doubanio.com" && \
    pip install --upgrade pip setuptools wheel && \
    pip install -r /tmp/qasystem/requirements.txt && \
    pip check

# Install Agent with dependency resolution enabled. This makes a future
# pyproject dependency change fail the image build instead of being hidden.
COPY agent /app/agent
RUN pip install "/app/agent[api]" && pip check

# Keep the image independently runnable. Compose must not shadow /app with an
# older host-side source tree.
COPY backend /app/backend
COPY algorithm /app/algorithm
COPY mcp_services /app/mcp_services
COPY docker/verify_runtime.py /usr/local/bin/verify_qasystem_runtime.py

COPY --from=opencode-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-runtime /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV PYTHONPATH=/app:/app/agent \
    LIBREOFFICE_PATH=/usr/bin/soffice

# Validate dependencies, native programs and both ASGI entry points while the
# image is being built. Any missing runtime component aborts the build.
RUN python /usr/local/bin/verify_qasystem_runtime.py

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

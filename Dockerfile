# QASystem source-free environment image. Application source and models are
# mounted by Compose at runtime and are intentionally absent from this image.

ARG NODE_IMAGE=node:20-bullseye-slim
ARG MAVEN_IMAGE=maven:3.9.9-eclipse-temurin-17
ARG VLLM_IMAGE=vllm/vllm-openai:v0.31.0
FROM ${NODE_IMAGE} AS opencode-runtime

ARG OPENCODE_VERSION=1.17.15
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && \
    test "$(opencode --version)" = "${OPENCODE_VERSION}"

FROM ${MAVEN_IMAGE} AS ofd-converter-builder

WORKDIR /build
COPY ofd_converter/pom.xml /build/pom.xml
COPY ofd_converter/src /build/src
RUN mvn --batch-mode --no-transfer-progress package -DskipTests

FROM ${VLLM_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# 底座是 Ubuntu 22.04（jammy），默认 apt 源走 http://archive.ubuntu.com。国内/代理
# 网络下 80 端口会被中间设备回 502 Bad Gateway（同时 https 的 nvidia/launchpad 源
# 正常），因此统一换成国内 HTTPS 镜像源；需要换源时用 --build-arg UBUNTU_MIRROR=... 覆盖。
ARG UBUNTU_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu
RUN find /etc/apt -maxdepth 2 \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e "s@http://archive.ubuntu.com/ubuntu@${UBUNTU_MIRROR}@g" \
        -e "s@http://security.ubuntu.com/ubuntu@${UBUNTU_MIRROR}@g" {} + && \
    apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libcairo2-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libmagic1 \
    libreoffice-core \
    libreoffice-impress \
    openjdk-17-jre-headless \
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
    python-is-python3 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependency contracts only; ordinary source changes continue to use
# Docker's dependency cache without adding application source to the image.
#
# All services share the same Python dependency graph; no conflicts are waived.
COPY backend/requirements.txt /tmp/qasystem/requirements.txt
RUN pip config set global.index-url "$PIP_INDEX_URL" && \
    pip config set global.extra-index-url "$PIP_EXTRA_INDEX_URL" && \
    pip config set global.trusted-host "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com" && \
    pip install --retries 8 --timeout 120 --upgrade pip "setuptools>=77,<81" wheel pycairo && \
    pip install --retries 8 --timeout 120 -r /tmp/qasystem/requirements.txt && \
    pip check

# MinerU publishes its pipeline/VLM dependencies as optional extras. Keep this
# layer separate so ordinary application changes reuse the heavy base layer.
# 这一层要下上 GB 的 wheel，单独挂 BuildKit 的 pip 下载缓存（不进镜像）。全局
# PIP_NO_CACHE_DIR=1 是关缓存的，所以这里用 env -u 取消它，重跑时不必重新下载。
COPY backend/requirements.mineru-extras.txt /tmp/qasystem/requirements.mineru-extras.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR pip install \
    --retries 8 --timeout 120 -r /tmp/qasystem/requirements.mineru-extras.txt && \
    pip check

# Read Agent dependencies from their canonical pyproject without copying the
# Agent package into the environment image. Runtime source is mounted later.
COPY agent/pyproject.toml /tmp/qasystem/agent-pyproject.toml
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('/tmp/qasystem/agent-pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install',*(p['dependencies']+p['optional-dependencies']['api'])])" && \
    pip check

COPY --from=ofd-converter-builder /build/target/ofd-to-pdf-1.0.0.jar /opt/qasystem/ofdrw/ofd-to-pdf.jar

# Clef and NanoJev reuse the verified Backend Torch/Transformers runtime.
# Keep the dependency contract without installing a second CUDA/Torch stack.
# Only requirements enter this layer; source and weights are mounted at runtime.
COPY clef_service/requirements.txt /tmp/qasystem/clef-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR python -m pip install --retries 8 --timeout 120 -r /tmp/qasystem/clef-requirements.txt && \
    pip check && \
    python -c "import torch, torchvision, transformers, accelerate, safetensors, fastapi, uvicorn; from transformers import Qwen3_5ForConditionalGeneration"

COPY --from=opencode-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-runtime /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Application-aware verification is performed by build-image.ps1 with the
# repository source mounted read-only; metrics instrumentation must work.
RUN python -c "from fastapi import FastAPI; from prometheus_fastapi_instrumentator import Instrumentator; Instrumentator().instrument(FastAPI()); print('instrumentator ok')" && \
    python -c "import fastapi, torch, vllm, cv2, redis, mcp, openai" && \
    python -c "import mineru, docling, transformers; from importlib.metadata import version; \
    assert version('mineru').startswith('4.'), version('mineru'); \
    assert version('transformers').startswith('5.'), version('transformers'); \
    from mineru import parse" && \
    node --version && \
    opencode --version && \
    test -x /usr/bin/soffice && \
    test -f /opt/qasystem/ofdrw/ofd-to-pdf.jar && \
    command -v tini >/dev/null

# 基础镜像 vllm/vllm-openai 自带 ENTRYPOINT（vllm.entrypoints.openai.api_server）。
# Compose 只覆盖 command（= CMD），不会覆盖 ENTRYPOINT，若不显式复位，两个服务的
# command 会被当成 vLLM 的参数追加，容器根本起不到 supervisor/agent。
# tini 已在上面安装、并在 docker/verify_runtime.py 的程序清单中断言存在。
ENTRYPOINT ["/usr/bin/tini", "--"]

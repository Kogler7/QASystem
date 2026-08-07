# QASystem dependency base. Application source and deployment configuration
# belong to Dockerfile.runtime and are intentionally absent from this image.

ARG NODE_IMAGE=node:20-bullseye-slim
ARG MAVEN_IMAGE=maven:3.9.9-eclipse-temurin-17
ARG VLLM_IMAGE=vllm/vllm-openai:v0.11.2
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

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libcairo2-dev \
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the heavy Python layer before copying application code so normal
# source changes continue to use Docker's dependency cache.
COPY backend/requirements.txt /tmp/qasystem/requirements.txt
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.extra-index-url "https://mirrors.aliyun.com/pypi/simple/ https://pypi.doubanio.com/simple/" && \
    pip config set global.trusted-host "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com pypi.doubanio.com" && \
    pip install --upgrade pip "setuptools>=77,<81" wheel pycairo && \
    pip install -r /tmp/qasystem/requirements.txt && \
    pip check

# MinerU publishes its pipeline/VLM dependencies as optional extras. Keep this
# layer separate so ordinary application changes reuse the heavy base layer.
COPY backend/requirements.mineru-extras.txt /tmp/qasystem/requirements.mineru-extras.txt
RUN pip install --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    --retries 8 --timeout 120 -r /tmp/qasystem/requirements.mineru-extras.txt && \
    pip check

# Read Agent dependencies from their canonical pyproject without copying the
# Agent package into the dependency base. Dockerfile.runtime installs it.
COPY agent/pyproject.toml /tmp/qasystem/agent-pyproject.toml
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('/tmp/qasystem/agent-pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install',*(p['dependencies']+p['optional-dependencies']['api'])])" && \
    pip check

COPY --from=ofd-converter-builder /build/target/ofd-to-pdf-1.0.0.jar /opt/qasystem/ofdrw/ofd-to-pdf.jar

COPY --from=opencode-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-runtime /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# The base is not deployable. Keep only a dependency smoke test here; the
# application-aware verifier runs while Dockerfile.runtime is built.
RUN python -c "import fastapi, torch, vllm, cv2, redis, mcp, openai" && \
    test -x /usr/bin/soffice && \
    test -f /opt/qasystem/ofdrw/ofd-to-pdf.jar && \
    command -v tini >/dev/null

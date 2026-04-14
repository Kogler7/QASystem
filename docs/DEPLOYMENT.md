# QASystem Docker 完整部署手册（跨服务器）

本文档面向生产/准生产部署，目标是把 `qasystem` 从当前开发机部署到另一台服务器，并满足以下要求：

- 使用 Docker Compose 一次性拉起前后端与基础服务
- 模型文件外置（不进仓库、不打进镜像）
- 可选使用内置或外部 MySQL / Neo4j / Milvus
- 提供可执行的上线、验证、升级、回滚与排障步骤

---

## 1. 部署架构与原则

### 1.1 服务架构

默认 Compose 栈（仓库根 `docker-compose.yml`）：

- `frontend`：Nginx 托管前端静态资源，反代 `/api` 到 backend
- `backend`：FastAPI + Uvicorn，进程内调用 `algorithm/`
- `db`：MySQL 8（默认开启）
- `neo4j`：可选 profile（`--profile neo4j` 启用）

可选外部依赖：

- Milvus（意图识别）
- 外部 Neo4j（替代 compose profile）
- 外部 MySQL（替代 compose `db`）
- 外部 LLM 服务（vLLM / Ollama / OpenAI 兼容）

### 1.2 最佳实践

- 模型文件放宿主机目录，例如 `/opt/qasystem-models`
- 通过 volume 只读挂载到 backend 容器 `/models`
- 使用环境变量配置模型路径（不在代码里写机器绝对路径）
- 代码与数据分离：镜像只放代码和依赖，不放模型与业务数据

---

## 2. 服务器准备

## 2.1 最低建议配置

- CPU：8 核及以上
- 内存：16 GB 及以上（模型相关场景建议更高）
- 磁盘：100 GB 及以上（日志、镜像、数据卷、模型）
- OS：Linux（Ubuntu 20.04/22.04/24.04 均可）

## 2.2 安装 Docker 与 Compose

```bash
docker --version
docker compose version
```

若未安装，请先按官方文档安装 Docker Engine 和 Compose Plugin。

## 2.3 目录规划（推荐）

```bash
sudo mkdir -p /srv/qasystem
sudo mkdir -p /opt/qasystem-models
```

- `/srv/qasystem`：代码与 compose 目录
- `/opt/qasystem-models`：外置模型目录（挂载到容器 `/models`）

---

## 3. 代码交付与镜像发布（两条流水线）

本节是“把部署材料送到目标机”的步骤，不是最终运行步骤。  
最终运行统一在第 7 节执行 `docker compose up`。

## 3.1 流水线 A：目标机在线拉代码并本地构建（推荐）

适用场景：

- 目标机能访问 Git 仓库与镜像源
- 希望流程简单、后续升级方便（`git pull`）

### A-1 在目标机拉代码

```bash
cd /srv
git clone <your-repo-url> qasystem
# git clone git@10.126.62.88:qasystem/qasystem.git qasystem
cd /srv/qasystem
```

### A-2 准备配置与模型目录

```bash
cp .env.example .env
mkdir -p /opt/qasystem-models
```

按第 5 节编辑 `.env`（尤其是 `JWT_SECRET`、`DIFY_*`、`HOST_MODELS_DIR` 等）。

### A-3 在目标机构建并启动

```bash
docker compose up -d --build
```

若要启用 compose 内 Neo4j：

```bash
docker compose --profile neo4j up -d --build
```

## 3.2 流水线 B：源机预构建镜像，目标机离线加载

适用场景：

- 目标机不能访问外网或拉依赖慢
- 希望固定镜像版本进行交付

### B-1 在源机构建镜像

```bash
cd /path/to/qasystem
docker compose build
docker image ls | rg "railway_(frontend|backend)|mysql|neo4j"
```

### B-2 在源机导出镜像包

```bash
docker save -o qasystem-images.tar railway_frontend railway_backend mysql:8.0 neo4j:5-community
```

### B-3 传输到目标机

示例（按实际用户/IP修改）：

```bash
scp qasystem-images.tar user@<target-ip>:/srv/
scp -r /path/to/qasystem user@<target-ip>:/srv/
```

### B-4 在目标机加载镜像

```bash
cd /srv
docker load -i qasystem-images.tar
```

### B-5 在目标机启动

```bash
cd /srv/qasystem
cp .env.example .env
# 按第 5 节填写 .env 后执行
docker compose up -d
```

若要启用 compose 内 Neo4j：

```bash
docker compose --profile neo4j up -d
```

> 说明：离线镜像方案通常不需要 `--build`，除非你在目标机改了代码或 Dockerfile。

---

## 4. 模型外置部署（重点）

## 4.1 宿主机模型目录结构示例

```text
/opt/qasystem-models
├── bge_m3_model
├── sbert_model
├── bge-small-zh-v1.5
└── hub/
    └── models/
        └── OpenDataLab/
            ├── PDF-Extract-Kit-1___0
            └── MinerU2___5-2509-1___2B
```

## 4.2 容器挂载约定

`docker-compose.yml` 已配置：

- `${HOST_MODELS_DIR:-/opt/qasystem-models}:/models:ro`

即：宿主机模型目录挂载为容器内 `/models`（只读）。

## 4.3 路径变量约定（Docker）

在根 `.env` 中：

- `MODEL_PATH=/models/bge_m3_model`
- `INTENT_MODEL_BASE_DIR=/models`
- `CONFIDENCE_SBERT_MODEL_PATH=/models/sbert_model`
- `NL2CYPHER_EMBEDDING_MODEL_PATH=/models/bge-small-zh-v1.5`
- `MINERU_MODEL_PIPELINE_PATH=/models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0`
- `MINERU_MODEL_VLM_PATH=/models/hub/models/OpenDataLab/MinerU2___5-2509-1___2B`

---

## 5. 环境变量配置（根 `.env`）

在目标机：

```bash
cd /srv/qasystem
cp .env.example .env
```

重点修改以下变量：

### 5.1 Compose 与端口

- `HTTP_PORT=80`
- `MYSQL_BIND=127.0.0.1`（生产建议保持内网）
- `HOST_MODELS_DIR=/opt/qasystem-models`
- `DATABASE_HOST=db`（外部 MySQL 可改为内网地址）
- `DATABASE_PORT=3306`

### 5.2 安全与认证

- `JWT_SECRET=<强随机字符串>`
- `ENVIRONMENT=production`

### 5.3 Dify

- `DIFY_API_URL=<.../v1>`
- `DIFY_API_KEY=...`
- `DIFY_KNOWLEDGE_API_KEY=...`
- （可选）`DIFY_USE_WORKFLOW=true` + 工作流相关变量

### 5.4 模型与算法

- `MODEL_PATH=/models/bge_m3_model`
- `INTENT_MODEL_BASE_DIR=/models`
- `MINERU_CONFIG_PATH=/app/algorithm/preproc/mineru.json`
- `MINERU_MODEL_PIPELINE_PATH=/models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0`
- `MINERU_MODEL_VLM_PATH=/models/hub/models/OpenDataLab/MinerU2___5-2509-1___2B`

### 5.5 外部服务地址（按实际网络）

- `NEO4J_URI`（使用 compose neo4j 时设为 `bolt://neo4j:7687`）
- `MILVUS_URI`
- `LLM_BASE_URL` / `OLLAMA_URL`

---

## 6. 数据库与中间件部署策略

## 6.1 MySQL（两种方案）

### 方案 A：使用 compose 内置 MySQL（默认）

- 无需额外部署，`db` 自动启动
- backend 会自动使用 `@db:3306` 连接

### 方案 B：使用外部 MySQL

1. 注释 `docker-compose.yml` 的 `db` 服务（或拆分 profile）
2. 将 backend 的 `DATABASE_URL` 改为外部地址（需修改 compose 配置）
3. 放行外部 MySQL 网络策略（仅后端可访问）

> 当前仓库 compose 默认强制拼接 `DATABASE_URL` 指向 `db`，若你要接外部 MySQL，需要同步调整 compose 的 backend `environment` 段。

## 6.2 Neo4j（两种方案）

### 方案 A：compose profile

```bash
docker compose --profile neo4j up -d --build
```

并在 `.env`：

- `NEO4J_URI=bolt://neo4j:7687`
- `NEO4J_USER=neo4j`
- `NEO4J_PASSWORD=<your-password>`

### 方案 B：外部 Neo4j

在 `.env`：

- `NEO4J_URI=bolt://host.docker.internal:7687`（同机宿主）
- 或 `NEO4J_URI=bolt://<private-ip>:7687`（局域网）

## 6.3 Milvus（建议外部独立部署）

可用仓库提供的 `milvus-docker-compose.yml` 独立启动：

```bash
cd /srv/qasystem
export DOCKER_VOLUME_DIRECTORY=.
docker compose -f milvus-docker-compose.yml up -d
```

然后在根 `.env`：

- `MILVUS_URI=http://host.docker.internal:19530`（同机宿主）
- 或对应可达地址

---

## 7. 启动与上线流程

## 7.1 首次启动

```bash
cd /srv/qasystem
docker compose up -d --build
```

如果需要 compose 内 Neo4j：

```bash
docker compose --profile neo4j up -d --build
```

## 7.2 查看状态与日志

```bash
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f db
```

---

## 8. 验收清单（上线必做）

## 8.1 基础连通

- 前端打开：`http://<server-ip>:<HTTP_PORT>/`
- 健康检查：`http://<server-ip>:<HTTP_PORT>/health`
- 返回 `{"status":"ok"}`

## 8.2 容器内模型可见性

```bash
docker compose exec backend ls /models
docker compose exec backend ls /models/hub/models/OpenDataLab
```

## 8.3 核心 API

- 聊天：`POST /api/chat`
- 文档管理：`/api/knowledge/documents`
- 登录注册：`/api/auth/login` / `/api/auth/register`

## 8.4 组件健康

至少验证以下组件接口：

- `document-preproc`
- `intent-recognition`
- `knowledge-extract`
- `kg-update`
- `nl2cypher`
- `answer-generation`

---

## 9. 升级、回滚与备份

## 9.1 升级流程

```bash
cd /srv/qasystem
git pull
docker compose up -d --build
```

## 9.2 快速回滚（建议）

- 使用 Git tag / release 管理版本
- 回滚到历史版本后重新 `docker compose up -d --build`
- 外置模型和数据卷不受代码回滚影响

## 9.3 备份建议

- MySQL：定期 `mysqldump`
- Neo4j：定期导出/快照
- 数据卷：`backend_documents`、`backend_preproc_output`
- `.env`：安全保管（含密钥）

---

## 10. 常见问题与处理

## 10.1 backend 启动慢/健康检查失败

- 首次镜像构建依赖较重，等待更久再看日志
- 检查 `docker compose logs -f backend`
- 确认 Dify/Neo4j/Milvus/LLM 地址可达

## 10.2 模型找不到

- 检查宿主机目录是否存在：`$HOST_MODELS_DIR`
- 检查挂载是否生效：`docker compose exec backend ls /models`
- 检查 `.env` 路径是否都使用容器路径（`/models/...`）

## 10.3 Mineru 模型路径问题

- 已支持环境变量覆盖 `mineru.json`：
  - `MINERU_MODEL_PIPELINE_PATH`
  - `MINERU_MODEL_VLM_PATH`
- 建议在 `.env` 中设置，不要改代码路径

## 10.4 连接宿主机服务失败

- Linux 下 backend 已配置 `host.docker.internal:host-gateway`
- 若仍不可达，改用宿主机内网 IP
- 检查防火墙 / 安全组放行

---

## 11. 生产安全建议

- 不暴露 MySQL、Neo4j、Milvus 到公网
- 前置反向代理（Nginx/Caddy）做 HTTPS 与访问控制
- 所有密钥用强随机值并定期轮换
- `.env` 不入库，按环境分发（dev/staging/prod）
- 模型目录只读挂载，避免误写

---

## 12. 一份可参考的部署顺序（建议）

1. 准备目标机 Docker 环境
2. 同步代码到 `/srv/qasystem`
3. 准备模型到 `/opt/qasystem-models`
4. 配置 `.env`（含 `HOST_MODELS_DIR` 与各类服务地址）
5. `docker compose up -d --build`（或加 `--profile neo4j`）
6. 完成验收清单并记录版本
7. 建立备份和监控策略

---

如需进一步规范化，我可以继续补一份：

- `prod.env.example`（生产模板）
- `staging.env.example`（预发模板）
- 一键检查脚本（启动前验证模型目录、端口连通性、关键变量完整性）


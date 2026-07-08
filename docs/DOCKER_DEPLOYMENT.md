# Docker 部署说明

本文说明如何在一台新机器上通过 Docker Compose 启动轨道交通知识服务系统，并配置后端依赖的数据库、图数据库、向量库、LLM 等外部服务。

## 1. 部署范围

仓库根目录的 `docker-compose.yml` 只负责启动：

- `backend`：FastAPI 后端，镜像包含 `backend/`、`algorithm/`、`agentcode/`
- `frontend`：Vue 前端，Nginx 托管静态资源

以下服务需要单独部署或已有可访问实例：

- MySQL
- Neo4j
- Milvus
- Elasticsearch
- OpenAI-compatible LLM 服务，例如 vLLM、Ollama OpenAI API、第三方大模型 API
- 可选：Dify

## 2. 新环境准备

### Linux

安装 Docker Engine 和 Compose 插件后，确认服务已启动：

```bash
sudo systemctl enable docker
sudo systemctl start docker
docker version
docker compose version
```

如需让当前用户直接运行 Docker：

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### Windows

安装并启动 Docker Desktop，确认 WSL2 后端可用，然后在 PowerShell 中检查：

```powershell
docker version
docker compose version
```

## 3. 获取代码

```bash
git clone <repo-url> qasystem
cd qasystem
```

如果是离线部署，也可以把代码目录和已构建镜像一起拷贝到目标机器。

## 4. 配置 `.env`

在仓库根目录复制配置模板：

```bash
cp .env.example .env
```

Docker Compose 部署只读取仓库根目录 `.env`。不要把本机调试用的 `backend/.env` 当作容器部署配置。

### 基础端口

```env
HTTP_PORT=80
BACKEND_BIND=127.0.0.1
BACKEND_HOST_PORT=8006
BACKEND_IMAGE=qasystem-backend:latest
FRONTEND_IMAGE=qasystem-frontend:latest
```

说明：

- 前端访问地址：`http://<服务器IP>:HTTP_PORT`
- 后端访问地址：`http://<服务器IP>:BACKEND_HOST_PORT`
- 如果需要让其他机器直接访问后端，把 `BACKEND_BIND` 改为 `0.0.0.0`

### MySQL

```env
MYSQL_DATABASE=railway_db
MYSQL_USER=root
MYSQL_PASSWORD=123456
DATABASE_HOST=host.docker.internal
DATABASE_PORT=3306
```

说明：

- MySQL 在宿主机上：`DATABASE_HOST=host.docker.internal`
- MySQL 在另一台机器上：`DATABASE_HOST=<MySQL服务器IP>`
- MySQL 与应用在同一个 Docker 网络中：填对应服务名

### Neo4j

```env
NEO4J_URI=bolt://host.docker.internal:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=neo4j2025
```

如果 Neo4j 在另一台机器上：

```env
NEO4J_URI=bolt://<Neo4j服务器IP>:7687
```

### LLM

本系统使用 OpenAI-compatible Chat Completions 接口。

本地或内网 vLLM 不需要 API Key 时：

```env
LLM_BACKEND=openai
LLM_BASE_URL=http://host.docker.internal:8003
LLM_API_VERSION=v1
LLM_MODEL=rule_v4
LLM_API_KEY=
```

云端或需要鉴权的 OpenAI-compatible 服务：

```env
LLM_BACKEND=openai
LLM_BASE_URL=https://<llm-api-host>/v1
LLM_API_VERSION=v1
LLM_MODEL=<model-name>
LLM_API_KEY=<api-key>
```

注意：

- 容器内访问宿主机服务时使用 `host.docker.internal`
- 知识沉淀接口的知识抽取步骤依赖 LLM；LLM 不可达时接口会返回 `code=1002`

### Milvus

```env
MILVUS_URI=http://host.docker.internal:19530
MILVUS_CONSISTENCY_LEVEL=Strong
COLLECTION_NAME=intent_rag
```

### Elasticsearch

```env
CONFIDENCE_ES_HOSTS=http://host.docker.internal:9200
CONFIDENCE_SBERT_MODEL_PATH=/models/sbert_model
```

### 模型目录

后端容器会把宿主机模型目录只读挂载到 `/models`：

```env
HOST_MODELS_DIR=./models
MODEL_PATH=/models/bge_m3_model
INTENT_MODEL_BASE_DIR=/models
NL2CYPHER_EMBEDDING_MODEL_PATH=/models/bge-small-zh-v1.5
CONFIDENCE_SBERT_MODEL_PATH=/models/sbert_model
```

请确保宿主机目录存在，例如：

```bash
mkdir -p models
```

如部署机模型放在 `/data/models`：

```env
HOST_MODELS_DIR=/data/models
```

## 5. 构建镜像

在仓库根目录执行：

```bash
docker compose build backend frontend
```

后端镜像依赖较大，首次构建会下载 Torch、CUDA、文档解析等依赖，耗时较长，镜像约 12GB 以上。

也可以直接：

```bash
docker compose build
```

## 6. 启动服务

```bash
docker compose up -d
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f backend
docker compose logs -f frontend
```

停止服务：

```bash
docker compose down
```

## 7. 健康检查

后端健康检查：

```bash
curl http://127.0.0.1:8006/health
```

预期返回：

```json
{"status":"ok"}
```

如果前端端口为 80：

```bash
curl http://127.0.0.1/
```

## 8. 协议接口验证

当前知识地图算法协议包含 4 个接口。

### 单文本向量化

```bash
curl -X POST http://127.0.0.1:8006/api/component/nl2cypher/text-to-vector \
  -H "Content-Type: application/json" \
  -d '{"text":"知识空间测试"}'
```

成功时返回 `code=0`，`data.vector` 为向量数组。

### 多文本向量化

```bash
curl -X POST http://127.0.0.1:8006/api/component/nl2cypher/text-to-vectors \
  -H "Content-Type: application/json" \
  -d '{"texts":["知识空间测试","第二段文本"]}'
```

成功时返回 `code=0`，`data.count` 为文本数量。

### 置信度更新

```bash
curl -X POST http://127.0.0.1:8006/api/component/kg-update/calculate-confidence \
  -H "Content-Type: application/json" \
  -d '{"graph":{"nodes":[],"relationships":[]}}'
```

成功时返回 `code=0`。如果传入真实关系，接口会尝试使用 Elasticsearch 和 Neo4j 计算证据。

### 知识沉淀

准备一个测试 CSV：

```bash
cat > knowledge_test.csv <<'EOF'
name,description
招采公开项目,用于测试知识沉淀接口
EOF
```

调用接口：

```bash
curl -X POST http://127.0.0.1:8006/api/knowledge/precipitation \
  -F "file=@knowledge_test.csv;filename=knowledge_test.csv" \
  -F "lang=ch" \
  -F "chunk_type=1" \
  -F "chunk_size=500" \
  -F "save_to_db=false"
```

说明：

- 该接口会执行文档解析、知识抽取、文本切片、向量化
- 知识抽取依赖 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`
- LLM 不可达时会返回 `code=1002`
- 完整成功时返回 `code=0`，并包含 `origin_url`、`vector_url`、`extract_url`、`graphs`

## 9. 离线镜像迁移

在有网络的机器构建后导出镜像：

```bash
docker save qasystem-backend:latest -o qasystem-backend.tar
docker save qasystem-frontend:latest -o qasystem-frontend.tar
```

拷贝到目标机器后导入：

```bash
docker load -i qasystem-backend.tar
docker load -i qasystem-frontend.tar
```

确认镜像：

```bash
docker images | grep qasystem
```

然后在目标机器准备代码、`.env`、模型目录，执行：

```bash
docker compose up -d
```

## 10. 常见问题

### 后端健康检查正常，但业务接口失败

先看后端日志：

```bash
docker compose logs --tail=200 backend
```

常见原因：

- MySQL 未启动或 `DATABASE_HOST` 不对
- LLM 地址不可达或 API Key 缺失
- Neo4j / Milvus / Elasticsearch 地址不对
- 模型目录没有挂载到 `/models`

### 容器里访问不了宿主机服务

Compose 已配置：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

部署时应在 `.env` 中使用：

```env
LLM_BASE_URL=http://host.docker.internal:<port>
NEO4J_URI=bolt://host.docker.internal:7687
MILVUS_URI=http://host.docker.internal:19530
CONFIDENCE_ES_HOSTS=http://host.docker.internal:9200
```

如果服务在另一台机器上，改为对应服务器 IP。

### LLM 没有 API Key 能不能用

可以，但前提是 `LLM_BASE_URL` 指向的本地或内网 OpenAI-compatible 服务不要求鉴权。

如果使用云端服务，通常必须填写：

```env
LLM_API_KEY=<api-key>
```

### 修改 `.env` 后不生效

重启服务：

```bash
docker compose up -d --force-recreate backend
```

如果改了代码或依赖，需要重新构建：

```bash
docker compose build backend
docker compose up -d backend
```


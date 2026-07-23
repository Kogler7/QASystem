# `.env` 迁移说明

本次更新将 Docling、MinerU 和向量模型统一改为外部 `models/` 挂载目录，并将 Agent API 统一通过 Backend 的 `8006` 端口发布。请在目标服务器保留现有 `.env` 中的数据库、Neo4j、Elasticsearch、LLM、端口和密钥配置，只更新以下模型项。

```env
# 指向随本次更新复制到服务器的 models 目录。
HOST_MODELS_DIR=./models

# MinerU 仅使用本地模型，不在运行时下载。
MINERU_MODEL_SOURCE=local
MINERU_MODEL_PIPELINE_PATH=/models/mineru/pipeline
MINERU_MODEL_VLM_PATH=/models/mineru/vlm

# 向量上传接口要求 512 维，三个用途统一使用 bge-small-zh-v1.5。
VECTOR_MODEL_PATH=/models/bge-small-zh-v1.5
CONFIDENCE_SBERT_MODEL_PATH=/models/bge-small-zh-v1.5
NL2CYPHER_EMBEDDING_MODEL_PATH=/models/bge-small-zh-v1.5
```

不要设置 `DOCLING_ARTIFACTS_PATH`。Docling 固定从容器内的 `/models/docling-models` 读取模型。

`MCP_PORT`、`AGENT_BIND` 和 `AGENT_HOST_PORT` 不再使用，可从现有 `.env` 删除。MCP 地址为 `http://<服务器地址>:${BACKEND_HOST_PORT}/mcp`，Agent 地址为 `http://<服务器地址>:${BACKEND_HOST_PORT}/agent`。

更新顺序：

```powershell
Copy-Item .env .env.backup
# 将上述模型项合并到现有 .env，不要覆盖外部服务和密钥配置。
docker load -i .\qasystem-backend-latest.tar
docker compose up -d --force-recreate backend agent
curl 'http://127.0.0.1:8006/health?full=true'
curl http://127.0.0.1:8006/agent/health
```

`full=true` 返回的 `docling`、`mineru_pipeline`、`mineru_vlm`、`vector_model_path`、`confidence_sbert_model_path` 和 `nl2cypher_embedding_model_path` 必须为 `ok: true`。MySQL、Neo4j、Elasticsearch、知识抽取 LLM 和文件服务会按目标服务器的现有网络配置检查连通性。

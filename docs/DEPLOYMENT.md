# 部署文档

本文档基于当前仓库代码与配置，按模块给出可执行的部署方案，适用于以下目标：

- 最小可用：前端 + 后端 + MySQL + Dify
- 完整能力：在最小可用基础上增加 Neo4j、Milvus、LLM 推理服务及算法依赖

## 1. 模块拓扑与依赖


| 模块       | 目录                                | 运行方式                    | 依赖                               |
| -------- | --------------------------------- | ----------------------- | -------------------------------- |
| 前端       | `frontend`                        | Vite（开发）/ Nginx（Docker） | 后端 API                           |
| 后端       | `backend`                         | FastAPI(Uvicorn)        | MySQL、Dify、`algorithm` 目录（进程内调用） |
| 基础数据库    | Compose `db`                      | MySQL 8.0               | 无                                |
| 图数据库（可选） | Compose `neo4j` 或外部服务             | Neo4j 5                 | 图谱相关组件                           |
| 向量库（可选）  | `milvus-docker-compose.yml` 或外部服务 | Milvus                  | 意图识别                             |
| 算法模块     | `algorithm/*`                     | 被后端进程内导入                | 视子模块而定（LLM/Neo4j/Milvus）         |


关键约束：

- 后端不是独立调用算法服务，而是**进程内导入** `algorithm/`，因此部署时必须保持仓库结构完整。
- Docker 后端镜像通过 `docker/backend.Dockerfile` 将 `algorithm/` 一起打包。

## 2. 部署模式选择

### 模式 A：一键 Docker（推荐上手）

适合：快速上线或联调。

```bash
cd /home/ycheng/ZhCHRAG/qasystem
cp .env.example .env
docker compose up -d --build
```

包含：`frontend + backend + db`。  
如需 Compose 内 Neo4j：

```bash
docker compose --profile neo4j up -d --build
```

### 模式 B：本机开发（后端+前端分开启动）

适合：开发调试、快速改代码。

```bash
cd /home/ycheng/ZhCHRAG/qasystem/backend
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

另开终端：

```bash
cd /home/ycheng/ZhCHRAG/qasystem/frontend
npm install
npm run dev
```

## 3. 基础服务模块部署

### 3.1 MySQL 模块（必选）

- Docker Compose 已默认创建 `db` 服务。
- 若使用外部 MySQL，确保后端 `DATABASE_URL` 指向外部地址。
- 默认端口映射为 `127.0.0.1:3306`，生产环境建议不暴露公网。

检查：

- `docker compose ps` 中 `db` 为 `healthy`
- 后端健康检查可通过（见第 7 节）

### 3.2 Dify 模块（必选）

后端依赖以下变量访问 Dify：

- `DIFY_API_URL`（必须带 `/v1`）
- `DIFY_API_KEY`
- `DIFY_KNOWLEDGE_API_KEY`

可选工作流变量：

- `DIFY_USE_WORKFLOW`
- `DIFY_WORKFLOW_API_KEY`
- `DIFY_WORKFLOW_INPUT_VAR`
- `DIFY_WORKFLOW_OUTPUT_VAR`

### 3.3 Neo4j 模块（图谱能力可选）

用途：答案生成、知识图谱更新、知识抽取落库、冲突检测等。

配置：

- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASSWORD`

Docker 场景：

- 使用 Compose 内 Neo4j 时：`NEO4J_URI=bolt://neo4j:7687`
- 使用宿主机 Neo4j 时：`NEO4J_URI=bolt://host.docker.internal:7687`

### 3.4 Milvus 模块（意图识别可选）

启动本地 Milvus：

```bash
cd /home/ycheng/ZhCHRAG/qasystem
export DOCKER_VOLUME_DIRECTORY=.
docker compose -f milvus-docker-compose.yml up -d
```

配置：

- `MILVUS_URI`（例如 `http://host.docker.internal:19530`）
- `COLLECTION_NAME`（默认 `intent_rag`）

### 3.5 LLM 推理模块（多算法共用）

统一通过 `algorithm/common` 读取：

- `LLM_BACKEND`（`openai` / `ollama` / `zhipu`）
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_API_KEY`

常见组合：

- 本地 vLLM：`LLM_BACKEND=openai` + OpenAI 兼容地址
- Ollama：配置 `OLLAMA_URL`（NL2Cypher 常用）

## 4. 后端模块部署（`backend`）

### 4.1 配置文件优先级

后端加载顺序：

1. `backend/.env`（override=True）
2. 仓库根 `.env`（override=False，只补缺）

建议：

- 本机运行：以 `backend/.env` 为主
- Docker 运行：以仓库根 `.env` 为主（`env_file` 注入容器）

### 4.2 必配项

- `DATABASE_URL`
- `JWT_SECRET`、`JWT_ALGORITHM`
- `DIFY_API_URL`、`DIFY_API_KEY`

### 4.3 Docker 构建特性

`docker/backend.Dockerfile` 会：

- 安装 `backend/requirements.txt`（体积较大、耗时较长）
- 拷贝 `backend/app`
- 拷贝 `algorithm`（保证组件模块可被导入）

### 4.4 启动与验证

- 本机：`uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Docker：由 compose 自动启动

健康检查：

- `GET /health` 返回 `{"status":"ok"}`

## 5. 前端模块部署（`frontend`）

### 5.1 开发模式

- `npm run dev` 默认 `http://localhost:5173`
- `vite.config.js` 代理 `/api` 到 `VITE_API_PROXY_TARGET`（默认 `http://localhost:8000`）

### 5.2 生产模式（Docker）

- `Dockerfile.frontend` 多阶段构建 + Nginx
- 通过根 `docker-compose.yml` 暴露 `HTTP_PORT`（默认 80）
- Nginx 将 `/api` 反向代理到 backend 容器

## 6. 算法组件模块部署（按功能启用）

说明：算法模块由后端进程内调用，以下依赖按需安装/启用；未满足条件时接口通常返回“模块不可用”。

### 6.1 文档预处理（`algorithm/preproc`）

用途：PDF/DOCX/Excel/HTML 转 Markdown 或文本。  
依赖：`algorithm/preproc/requirements.txt`。  
可选变量：

- `MINERU_CONFIG_PATH`
- `MINERU_MODEL_SOURCE`

后端接口：

- `GET /api/component/document-preproc/health`
- `POST /api/component/document-preproc/convert`

### 6.2 知识抽取（`algorithm/uie`）

用途：文本抽取结构化信息，可写入 Neo4j。  
关键依赖：`openai`、`neo4j`（后端大依赖一般已覆盖）。  
关键前提：Neo4j 可连接，LLM 可用。  
相关变量：`UIE_*`、`NEO4J_*`、`LLM_*`。

### 6.3 知识图谱更新（`algorithm/KGUpdate`）

用途：三元组批量添加/删除与图谱统计。  
前提：Neo4j 可连接。  
配置来源：`algorithm/KGUpdate/config.json`，并可通过环境变量覆盖部分阈值（如 `KG_CONFIDENCE_THRESHOLD`）。

### 6.4 NL2Cypher（`algorithm/NL_to_cypher`）

用途：自然语言问题转 Cypher。  
依赖：`algorithm/NL_to_cypher/requirements.txt`。  
常用变量：

- `OLLAMA_URL`（若走 Ollama）
- `NL2CYPHER_CHROMA_DB_PATH`
- `NL2CYPHER_EMBEDDING_MODEL_PATH`
- `NL2CYPHER_RECORDS_JSON`

### 6.5 意图识别（`algorithm/intent_recognition_model`）

用途：Milvus 混合检索 + LLM 结构化输出。  
依赖：`algorithm/intent_recognition_model/requirements.txt`。  
必要条件：

- Milvus 可用（`MILVUS_URI`）
- 模型路径有效（`MODEL_PATH` 或 `INTENT_MODEL_BASE_DIR`）
- LLM 可用（`LLM_*` 或 `DEEPSEEK_API_KEY`）

建议变量：

- `MILVUS_URI`
- `COLLECTION_NAME`
- `MODEL_PATH`
- `DATA_DIR`

接口检查：

- `GET /api/component/intent-recognition/health`

### 6.6 答案生成（`algorithm/ans`）

用途：基于 Neo4j 的图谱问答求解。  
依赖：`algorithm/ans/requirements.txt` 与 `algorithm/ans/QA/requirements.txt`。  
前提：Neo4j + LLM 配置可用。  
相关变量：`NEO4J_*`、`LLM_*`（或 QA 子模块配置）。

### 6.7 冲突检测（`algorithm/conflict_detection`，按需）

依赖：`algorithm/conflict_detection/requirements.txt`。  
前提：Neo4j 可用。  
可使用：

- `CONFLICT_NEO4J_URI`
- `CONFLICT_NEO4J_USER`
- `CONFLICT_NEO4J_PWD`

## 7. 模块级验收清单

### 7.1 基础联通

- `GET /health` 返回 `ok`
- 前端首页可打开，问答页面可请求后端
- `docker compose ps` 无异常重启

### 7.2 后端核心 API

- `POST /api/chat` 可返回响应
- 知识库文档上传/查询可用
- 登录注册接口可用

### 7.3 组件可用性

逐个访问组件健康检查或试跑接口，至少覆盖：

- `document-preproc`
- `intent-recognition`
- `knowledge-extract`
- `kg-update`
- `nl2cypher`
- `answer-generation`

若返回“模块不可用”，优先检查：

- 对应算法依赖是否安装
- 外部服务（Neo4j/Milvus/LLM）是否可达
- 环境变量是否在当前运行模式（Docker 或本机）中正确注入

## 8. 两套推荐落地方案

### 8.1 最小可用方案（建议第一阶段）

启用模块：

- `db`、`backend`、`frontend`、Dify

不启用或暂缓：

- Neo4j、Milvus、重型算法组件

价值：最快打通用户登录、知识库文档管理、基础问答链路。

### 8.2 完整能力方案（建议第二阶段）

在最小可用基础上增加：

- Neo4j
- Milvus
- 统一 LLM 服务（vLLM/Ollama/OpenAI 兼容）
- 组件算法所需依赖与模型文件

价值：完整覆盖组件管理里的抽取、图谱更新、意图识别、NL2Cypher、答案生成能力。

## 9. 运维常用命令

```bash
cd /home/ycheng/ZhCHRAG/qasystem
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
docker compose restart backend
docker compose down
# 清空卷（慎用）
docker compose down -v
```


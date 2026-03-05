# 轨道交通知识服务系统 - FastAPI 后端

本目录为前端项目配套的 FastAPI 后端，实现：

- 作为前端与 **Dify** 之间的中间层（对话与知识检索）
- 本地 **知识库文档管理**（上传、列表、删除）
- **知识抽取组件**（进程内调用 `algorithm/uie`，按函数封装，无需单独启动抽取服务）
- **知识图谱更新组件**（进程内调用 `algorithm/KGUpdate`，支持三元组批量添加/删除与图谱统计，配置见 `algorithm/KGUpdate/config.json`）
- **向量转化存储与自然语言转 Cypher 组件**（进程内调用 `algorithm/NL_to_cypher`，根据自然语言问题与图谱 Schema 生成 Cypher，依赖 Ollama，见 `algorithm/NL_to_cypher`）
- **意图识别组件**（进程内调用 `algorithm/intent_recognition_model`，Milvus 混合检索 + DeepSeek 结构化输出，配置见 `algorithm/intent_recognition_model/.env`）
- **答案生成组件**（进程内调用 `algorithm/ans`，Neo4j 知识图谱问答与问题求解器，配置见 `algorithm/ans/QA/config.py` 或算法内默认）
- 基础 **用户注册 / 登录 / 当前用户** 模块（JWT）

## 目录结构

```text
backend/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 入口
│   ├── config.py        # 配置（Dify / JWT / DB）
│   ├── db.py            # 数据库引擎与会话
│   ├── routers/         # API 路由
│   │   ├── chat.py      # 对话接口（转发 Dify）
│   │   ├── knowledge.py # 知识库：文档 CRUD、检索、同步 Dify
│   │   └── users.py     # 注册 / 登录 / 当前用户
│   ├── services/
│   │   ├── dify_client.py # Dify API 封装
│   │   └── auth.py       # 密码哈希、JWT
│   ├── models/
│   │   ├── user.py      # 用户模型
│   │   └── document.py  # 文档元数据模型
│   └── deps/
│       └── auth.py      # 依赖：get_current_user
├── data/
│   └── documents/       # 上传文档存储目录
├── .env.example
├── .dockerignore
├── Dockerfile
├── requirements.txt
└── README.md
```

## 环境配置

1. 复制并修改环境变量：

```bash
cd backend
cp .env.example .env
```

根据实际情况填入：

- `DIFY_API_URL` / `DIFY_API_KEY` / `DIFY_KNOWLEDGE_API_KEY`
- `JWT_SECRET` / `JWT_ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES`
- `DATABASE_URL`（默认示例为 MySQL + asyncmy）

## 安装依赖

### 使用 Python 3.10+

```bash
cd backend
pip install -r requirements.txt
```

若需使用 **知识抽取** 组件，还需安装 `algorithm/uie` 的依赖（如 openai、neo4j 等），见项目根目录下 [algorithm/uie/README.md](../algorithm/uie/README.md)。未安装时，抽取接口会返回“模块不可用”，其余接口不受影响。

若需使用 **知识图谱更新** 组件，需保证 Neo4j 可连通，并在 `algorithm/KGUpdate/config.json` 中配置 `neo4j`（uri/username/password/database）与 `system.confidence_threshold`。后端已依赖 `neo4j`，无需额外安装。

若需使用 **向量转化存储与自然语言转 Cypher** 组件，需安装 `algorithm/NL_to_cypher/requirements.txt` 中的依赖（如 langchain、requests 等），并启动 Ollama 服务。可通过环境变量 `OLLAMA_URL`、`OLLAMA_MODEL_NAME` 覆盖默认 Ollama 地址与模型。未安装时，该组件接口返回“模块不可用”。

若需使用 **意图识别** 组件，需安装 `algorithm/intent_recognition_model` 所需依赖（如 langchain-deepseek、pymilvus、milvus-model 等），并配置 `algorithm/intent_recognition_model/.env`（`MILVUS_URI`、`DEEPSEEK_API_KEY`、`MODEL_PATH`、`DATA_DIR`、`COLLECTION_NAME` 等），且 Milvus 已启动、意图集合已初始化。未配置或不可用时，该组件接口返回“模块不可用”。

若需使用 **答案生成** 组件，需安装 `algorithm/ans/QA/requirements.txt` 中的依赖（neo4j、openai、pyvis、networkx 等），并保证 Neo4j 可连通；Neo4j 与 LLM 参数见 `algorithm/ans/QA/answer_solve.py` 中默认配置或 `QA/config.py`。未配置或不可用时，该组件接口返回“模块不可用”。

```bash
# 仅使用知识抽取时按需安装，例如：
pip install openai neo4j
```

## 启动后端

**注意：** 前端 Vite 代理指向 `http://localhost:8000`，请使用端口 **8000** 启动，否则前端会连不上。

```bash
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

启动成功后：

- 健康检查：在浏览器或终端执行 `curl http://localhost:8000/health`，应返回 `{"status":"ok"}`
- 前端通过 `/api` 代理到本后端

## 与前端接口对齐说明

- 问答接口：`POST /api/chat`
  - 请求体：`{ "message": string, "history": Array }`
  - 响应体：`{ "success": true, "reply": string, "conversation_id": string | null, "error": null }`
- 知识库文档管理：
  - `GET /api/knowledge/documents`
  - `POST /api/knowledge/documents`（`multipart/form-data`，字段名 `file`）
  - `DELETE /api/knowledge/documents/{id}`
- 知识库检索：
  - `POST /api/knowledge/query`，请求体：`{ "query": string }`
  - 返回结构满足前端对 `data.results | data.list | data.data` 的兼容读取方式。

## 用户模块接口

- 注册：`POST /api/auth/register`
- 登录：`POST /api/auth/login`
- 当前用户：`GET /api/users/me`（需携带 `Authorization: Bearer <token>`）

## Docker 与根目录

- 全栈编排（MySQL + 本后端 + 前端）在**项目根目录**的 `docker-compose.yml`。
- 根目录 `.env.example` 为 Docker 部署用；本目录 `.env.example` 为本地开发用。


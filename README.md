# 轨道交通知识服务系统（Railway Assistant）

面向轨道交通领域的知识服务：**知识库与文档**、基于 **Dify** 的智能问答、以及可在**组件管理**中调用的算法能力（文档预处理、知识抽取、图谱更新、NL2Cypher、意图识别、图谱问答、冲突检测等）。

- **前后端分离**：Vue 3 + FastAPI。  
- **算法集成**：后端通过 `sys.path` **进程内**加载 `algorithm/`（非独立微服务）；部署时保持仓库内 `algorithm/` 与 `backend/` 的相对路径。

---

## 仓库顶层一览

```
qasystem/
├── frontend/
├── backend/
├── agentcode/             # Claude Code 风格单智能体运行时（独立可编辑包）
├── algorithm/
├── docker/
│   ├── backend.Dockerfile    # 后端镜像（构建上下文 = 仓库根目录）
│   └── README.md
├── docs/
├── docker-compose.yml        # MySQL + backend + frontend；可选 profile neo4j
├── milvus-docker-compose.yml # 可选：本地 Milvus
├── embedEtcd.yaml
├── user.yaml
├── .dockerignore
├── .env.example
├── run.sh
├── run_frontend.sh
├── LICENSE.txt
└── README.md
```

---

## Docker 部署

在**仓库根目录**：

```bash
cp .env.example .env
docker compose up -d --build
```

访问 `http://<主机>:${HTTP_PORT:-80}/`；健康检查 `http://<主机>/health`。  
可选 Neo4j：`docker compose --profile neo4j up -d --build`，并将 `.env` 中 `NEO4J_URI=bolt://neo4j:7687`。

后端镜像**包含** `algorithm/`；构建执行完整 `backend/requirements.txt`，**耗时长、镜像大**，请预留内存与磁盘。详见 [frontend/DEPLOY.md](frontend/DEPLOY.md)、[docker/README.md](docker/README.md)。

---

## 本地开发

```bash
cd backend && cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # 已含可编辑安装 agentcode[fastapi]
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

cd frontend && npm install && npm run dev
```

或使用 `./run.sh`、`./run_frontend.sh`。

`./run.sh` 已自动注入 `PYTHONPATH`，可直接在后端中导入：

```python
from agentcode import AgentRuntime
```

---

## 环境变量

| 场景 | 文件 |
|------|------|
| 本机 `uvicorn` | **`backend/.env`**（主配置，模板 `backend/.env.example`） |
| `docker compose` | 仓库根目录 **`.env`**（`env_file` 注入后端容器；模板根目录 `.env.example`） |
| 后端内加载顺序 | `backend/app/config.py`：**先** `backend/.env`（`override=True`）**再** 仓库根 `.env`（`override=False`，只补缺） |

算法子目录下的 `.env`（如 `algorithm/intent_recognition_model/.env`）**仅建议在单独跑该目录脚本时使用**；走后端 API 时请在 **`backend/.env`**（或 Docker 根 `.env`）写同一批变量。`algorithm/ans/QA/.env` 等同理，见各目录 `.env.example`。

---

## 模块要点

**前端**：`src/views/knowledge/`、`src/views/component/*/` + `manifest.js`；`src/api/modules/component/*.js`；`vite.config.js` 代理 `/api`。

**后端**：`app/main.py`、`app/config.py`、`app/routers/*`、`app/modules/component/*/`（各组件 `router.py` + `service.py`）。

**算法**：`preproc`、`uie`、`KGUpdate`、`NL_to_cypher`、`intent_recognition_model`、`ans/QA`、`common`、`confidence_calculate`、`conflict_detection`。

**组件映射**（前端 / 后端 / 算法目录名）：文档预处理 `document-preproc` / `document_preproc` / `preproc`；知识抽取 `knowledge-extract` / `knowledge_extract` / `uie`；图谱更新 `knowledge-graph-update` / `kg_update` / `KGUpdate`；NL2Cypher；意图识别；答案生成 `answer-generation` / `answer_generation` / `ans`。

---

## 文档索引

- [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)  
- [docs/DEPLOYMENT_BY_MODULE.md](docs/DEPLOYMENT_BY_MODULE.md)  
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)  
- [docs/DIFY_WORKFLOW_SETUP.md](docs/DIFY_WORKFLOW_SETUP.md)  
- [docs/TIMEOUT_DIAGNOSIS.md](docs/TIMEOUT_DIAGNOSIS.md)  
- [backend/README.md](backend/README.md)  
- [frontend/README.md](frontend/README.md)  
- [frontend/DEPLOY.md](frontend/DEPLOY.md)  

---

## 技术栈

后端：Python 3.10+（镜像 3.12）、FastAPI、SQLAlchemy（async）、MySQL。  
前端：Vue 3、Vite、Element Plus、Axios。  
外部：Dify、Neo4j、Milvus、LLM、Ollama、vLLM 等。

---

## IntentGPT/vLLM 意图识别实验模块

`wzc-dev` 分支补充了独立的 `intent/` 实验模块，用于基于 IntentGPT 思路做意图识别，并通过 vLLM API 调用远程或本地模型（默认示例为 Qwen/Qwen3-8B）。该模块包含配置、提示词、评估脚本和 API 封装，适合单独验证意图识别策略或沉淀到主系统的意图识别组件中。

关键入口：

- `intent/main.py`：实验主入口。
- `intent/predict_intent.py`：意图预测封装。
- `intent/intentgpt/utils/vllm_client.py`：vLLM API 客户端。
- `intent/configs/track.yaml`：示例配置。
- `intent/.env.example`：vLLM 相关环境变量模板。

常用环境变量：

```env
VLLM_API_URL=http://127.0.0.1:8000/v1/completions
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_API_KEY=
```

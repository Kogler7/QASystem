# 轨道交通知识服务系统（Railway Assistant）

基于轨道交通行业知识图谱与 Dify 智能体的知识服务系统，提供智能问答、知识库管理、组件管理等功能。项目采用前后端分离、模块化结构，支持多人在各自负责的组件下并行开发。

## 项目结构

```
qasystem/
├── frontend/                 # 前端（Vue 3 + Vite + Element Plus）
│   ├── src/
│   │   ├── views/component/  # 组件管理（各子组件独立目录 + manifest 自动注册）
│   │   ├── api/modules/component/
│   │   └── ...
│   └── README.md
├── backend/                  # 后端（FastAPI）
│   ├── app/
│   │   ├── modules/component/  # 组件管理（每个组件独立 router + service）
│   │   ├── routers/           # 对话、知识库、用户
│   │   └── ...
│   └── README.md
├── algorithm/                # 算法模块（进程内调用）
│   ├── preproc/             # 文档预处理
│   ├── uie/                 # 知识抽取
│   ├── KGUpdate/            # 知识图谱更新
│   ├── NL_to_cypher/        # 自然语言转 Cypher
│   ├── intent_recognition_model/
│   ├── ans/                 # 答案生成
│   └── common/              # 统一 LLM 调用
├── docs/
│   ├── PROJECT_STRUCTURE.md # 项目结构与模块划分
│   ├── MODULE_OWNERSHIP.md  # 模块负责人
│   ├── CONTRIBUTING.md      # 贡献与新增组件指南
│   ├── DIFY_WORKFLOW_SETUP.md
│   └── TIMEOUT_DIAGNOSIS.md
├── docker-compose.yml
└── README.md
```

详细结构见 [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)。

## 快速开始

### 本地开发

1. **后端**（端口 8000）  
   见 [backend/README.md](backend/README.md)：配置 `.env`、安装依赖、启动 `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`。

2. **前端**（端口 5173）  
   见 [frontend/README.md](frontend/README.md)：`npm install`、`npm run dev`。前端通过 Vite 代理将 `/api` 转发到后端。

3. **数据库**  
   需本地 MySQL（或 Docker 单独起 MySQL），在 `backend/.env` 中配置 `DATABASE_URL`。

4. **组件（可选）**  
   组件管理中的各组件（知识抽取、文档预处理等）已集成在主后端进程内，按需安装对应算法依赖即可。见 [backend/README.md](backend/README.md) 及各算法目录 README。

### Docker 一键部署

在项目根目录配置 `.env`（可复制根目录 `.env.example`）后执行：

```bash
docker compose up -d
```

详见 [frontend/DEPLOY.md](frontend/DEPLOY.md)。

## 主要功能

- **智能问答**：对接 Dify 聊天/工作流，支持多轮对话。
- **知识库**：文档上传、列表、删除、检索；可同步至 Dify 知识库。
- **组件管理**：文档预处理、知识抽取、知识图谱更新、NL2Cypher、意图识别、答案生成等组件的配置与调用入口。
- **用户与权限**：注册、登录、JWT 鉴权。

## 模块与开发

- **模块划分**：前端、后端、算法均按组件独立目录组织，便于多人并行开发。
- **新增组件**：见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)。
- **负责人**：见 [docs/MODULE_OWNERSHIP.md](docs/MODULE_OWNERSHIP.md)。

## 文档

- [项目结构与模块划分](docs/PROJECT_STRUCTURE.md)
- [模块负责人](docs/MODULE_OWNERSHIP.md)
- [贡献与新增组件](docs/CONTRIBUTING.md)
- [后端接口与配置](backend/README.md)
- [前端开发说明](frontend/README.md)
- [Docker 部署说明](frontend/DEPLOY.md)
- [Dify 工作流配置](docs/DIFY_WORKFLOW_SETUP.md)
- [超时与故障排查](docs/TIMEOUT_DIAGNOSIS.md)

## 技术栈

- **后端**：Python 3.12、FastAPI、SQLAlchemy（async）、MySQL
- **前端**：Vue 3、Vite、Vue Router、Pinia、Element Plus、Axios
- **外部**：Dify（对话/工作流/知识库）、Neo4j、Milvus 等

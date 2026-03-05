# 轨道交通知识服务系统（Railway Assistant）

基于轨道交通行业知识图谱与 Dify 智能体的知识服务系统，提供智能问答、知识库管理、组件管理等功能。

## 项目结构

```text
railway_assistant/
├── backend/                 # FastAPI 后端
│   ├── app/                  # 应用代码
│   ├── data/documents/       # 上传文档存储（可选纳入版本控制）
│   ├── .env.example
│   ├── Dockerfile
│   ├── requirements.txt
│   └── README.md
├── frontend/                 # Vue 3 + Vite 前端
│   ├── src/
│   ├── public/
│   ├── .env.example
│   ├── Dockerfile.frontend
│   ├── nginx.conf
│   └── README.md
├── docs/                     # 项目文档（Dify 配置、故障排查等）
├── docker-compose.yml        # 全栈 Docker 编排（MySQL + 后端 + 前端）
├── .env.example              # Docker 部署用（唯一）；本地开发用 backend/、frontend/ 下 .env.example
├── .gitignore
└── README.md                 # 本文件
```

## 快速开始

### 本地开发

1. **后端**（端口 8000）  
   见 [backend/README.md](backend/README.md)：配置 `.env`、安装依赖、启动 `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`。

2. **前端**（端口 5173）  
   见 [frontend/README.md](frontend/README.md)：`npm install`、`npm run dev`。前端通过 Vite 代理将 `/api` 转发到后端。

3. **数据库**  
   需本地 MySQL（或 Docker 单独起 MySQL），在 `backend/.env` 中配置 `DATABASE_URL`。

4. **知识抽取组件（可选）**  
   组件管理中的「知识抽取」已集成在主后端进程内，按函数调用 `algorithm/uie`，无需单独启动抽取服务。使用前需安装 UIE 依赖（见 [algorithm/uie/README.md](algorithm/uie/README.md)）。若写入 Neo4j，需先启动 Neo4j 服务。

### Docker 一键部署

在项目根目录配置 `.env`（可复制根目录 `.env.example`）后执行：

```bash
docker compose up -d
```

详见 [frontend/DEPLOY.md](frontend/DEPLOY.md)（部署说明以根目录为基准）。

## 主要功能

- **智能问答**：对接 Dify 聊天/工作流，支持多轮对话。
- **知识库**：文档上传、列表、删除、检索；可同步至 Dify 知识库。
- **组件管理**：知识抽取等组件的配置与调用入口。
- **用户与权限**：注册、登录、JWT 鉴权。

## 文档

- [后端接口与配置](backend/README.md)
- [前端开发说明](frontend/README.md)
- [Docker 部署说明](frontend/DEPLOY.md)
- [Dify 工作流配置](docs/DIFY_WORKFLOW_SETUP.md)
- [超时与故障排查](docs/TIMEOUT_DIAGNOSIS.md)

## 技术栈

- **后端**：Python 3.12、FastAPI、SQLAlchemy（async）、MySQL
- **前端**：Vue 3、Vite、Vue Router、Pinia、Element Plus、Axios
- **外部**：Dify（对话/工作流/知识库）

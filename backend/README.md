# 轨道交通知识服务系统 - FastAPI 后端

本目录为前端项目配套的 FastAPI 后端，实现：

- 作为前端与 **Dify** 之间的中间层（对话与知识检索）
- 本地 **知识库文档管理**（上传、列表、删除）
- 基础 **用户注册 / 登录 / 当前用户** 模块（JWT）

## 目录结构

```text
backend/
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── config.py        # 配置（含 Dify & JWT & DB）
│   ├── db.py            # 数据库初始化与会话
│   ├── routers/
│   │   ├── chat.py      # 对话接口（转发到 Dify）
│   │   ├── knowledge.py # 知识库相关接口
│   │   └── users.py     # 用户管理与认证接口
│   ├── services/
│   │   ├── dify_client.py # Dify API 封装
│   │   └── auth.py        # 鉴权/密码哈希/JWT
│   ├── models/
│   │   └── user.py      # 用户数据模型（SQLAlchemy）
│   └── deps/
│       └── auth.py      # 获取当前用户依赖
├── data/
│   └── documents/       # 本地存储上传文档
├── .env.example         # 环境变量示例
└── requirements.txt     # 依赖
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


# 轨道交通知识服务系统 - 前端

Vue 3 + Vite + Element Plus 单页应用，通过 `/api` 代理访问后端。

## 目录结构

```text
frontend/
├── public/              # 静态资源
├── src/
│   ├── api/             # 接口封装
│   │   ├── request.js   # Axios 实例（baseURL: /api）
│   │   ├── index.js     # 统一导出
│   │   └── modules/     # 按业务划分：chat、knowledge、component
│   ├── assets/          # 需构建的资源
│   ├── constants/       # 公共常量（如应用标题）
│   ├── router/          # Vue Router 配置
│   ├── utils/           # 工具函数（如 markdown 渲染）
│   └── views/           # 页面视图
│       ├── component/   # 组件管理（各子组件独立目录 + manifest 自动注册）
│       │   ├── config.js     # 自动收集 manifest
│       │   ├── document-preproc/
│       │   │   ├── manifest.js
│       │   │   └── index.vue
│       │   └── ...（其他组件）
│       └── knowledge/   # 知识库（概览、文档管理、检索）
├── .env.example
├── Dockerfile.frontend  # 多阶段构建 + Nginx
├── nginx.conf           # Nginx 配置（静态 + /api 反向代理）
├── vite.config.js
├── package.json
└── README.md
```

## 开发

```bash
npm install
npm run dev
```

默认前端 `http://localhost:5173`，API 通过 Vite 代理到 `http://localhost:8000`，请先启动后端。

## 构建与预览

```bash
npm run build
npm run preview
```

## 环境变量

可选复制 `.env.example` 为 `.env`。Docker 部署时环境变量在根目录 `.env` 中配置，见 [DEPLOY.md](DEPLOY.md)。

## 与后端接口对应

- 问答：`POST /api/chat`
- 知识库文档：`GET/POST/DELETE /api/knowledge/documents` 等
- 知识检索：`POST /api/knowledge/query`
- 认证：`POST /api/auth/register`、`POST /api/auth/login`、`GET /api/users/me`

详见 [后端 README](../backend/README.md)。

# 项目结构与模块划分

本文档描述轨道交通知识服务系统的目录结构与模块划分，便于不同开发人员在各自负责的模块下并行开发，互不干扰。

## 1. 顶层结构

```
qasystem/
├── frontend/           # 前端（Vue 3 + Vite）
├── backend/            # 后端（FastAPI）
├── algorithm/          # 算法模块（Python，进程内调用）
├── docker/             # 后端 Dockerfile 等（Compose 构建上下文为仓库根）
├── docs/               # 项目文档
├── docker-compose.yml  # 全栈编排（MySQL + backend + frontend，可选 profile neo4j）
└── README.md
```

## 2. 前端结构

```
frontend/
├── src/
│   ├── main.js
│   ├── App.vue
│   ├── router/
│   │   └── index.js         # 路由配置
│   ├── api/
│   │   ├── request.js       # Axios 实例
│   │   ├── index.js
│   │   └── modules/
│   │       ├── chat.js
│   │       ├── knowledge.js
│   │       └── component/   # 组件管理 API（按组件拆分）
│   │           ├── index.js
│   │           ├── document-preproc.js
│   │           ├── knowledge-extract.js
│   │           ├── kg-update.js
│   │           ├── nl2cypher.js
│   │           ├── intent-recognition.js
│   │           └── answer-generation.js
│   ├── views/
│   │   ├── HomeView.vue
│   │   ├── ChatView.vue
│   │   ├── knowledge/       # 知识库模块
│   │   └── component/       # 组件管理
│   │       ├── layout.vue
│   │       ├── index.vue
│   │       ├── config.js    # 自动收集各子组件的 manifest
│   │       ├── document-preproc/
│   │       │   ├── manifest.js   # 菜单项与路由（新增组件只需加此目录）
│   │       │   └── index.vue
│   │       ├── knowledge-extract/
│   │       ├── knowledge-graph-update/
│   │       ├── nl2cypher/
│   │       ├── intent-recognition/
│   │       └── answer-generation/
│   ├── constants/
│   └── utils/
├── package.json
└── vite.config.js
```

**前端组件模块约定**：每个组件拥有独立目录 `views/component/{组件id}/`，内含 `manifest.js`（菜单与路由）和 `index.vue`（页面）。`config.js` 通过 `import.meta.glob` 自动收集 manifest，**新增组件时无需修改 config.js**。

## 3. 后端结构

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py
│   ├── db.py
│   ├── routers/
│   │   ├── chat.py          # 对话（Dify 转发）
│   │   ├── knowledge.py     # 知识库
│   │   └── users.py         # 用户认证
│   ├── modules/
│   │   └── component/       # 组件管理（按组件拆分）
│   │       ├── __init__.py  # 导出 COMPONENT_ROUTERS
│   │       ├── document_preproc/
│   │       │   ├── __init__.py
│   │       │   ├── router.py
│   │       │   └── service.py
│   │       ├── knowledge_extract/
│   │       ├── kg_update/
│   │       ├── nl2cypher/
│   │       ├── intent_recognition/
│   │       └── answer_generation/
│   ├── services/            # 非组件共享服务
│   │   ├── dify_client.py
│   │   └── auth.py
│   ├── models/
│   └── deps/
├── data/
│   ├── documents/
│   └── preproc_output/
├── requirements.txt
└── （Docker 镜像定义在仓库 docker/backend.Dockerfile，构建上下文为仓库根目录）
```

**后端组件模块约定**：每个组件在 `app/modules/component/{组件名}/` 下拥有 `router.py` 与 `service.py`，`__init__.py` 导出 `router`。`modules/component/__init__.py` 汇总所有组件路由供 `main.py` 注册。

## 4. 算法模块结构

```
algorithm/
├── preproc/                    # 文档预处理（PDF/DOCX/Excel → Markdown）
├── uie/                        # 知识抽取（UIE）
├── KGUpdate/                   # 知识图谱更新
├── NL_to_cypher/               # 自然语言转 Cypher
├── intent_recognition_model/   # 意图识别
├── ans/                        # 答案生成（QA）
├── common/                     # 统一 LLM 调用（vLLM/OpenAI/Zhipu）
└── conflict_detection/         # 冲突检测（UpSHACL，可选）
```

每个算法模块独立目录，有自己的 `requirements.txt` 或依赖说明，后端通过 `sys.path` 进程内调用。

## 5. 组件与对应关系

| 组件 | 前端目录 | 后端目录 | 算法目录 |
|------|----------|----------|----------|
| 文档预处理 | `views/component/document-preproc/` | `modules/component/document_preproc/` | `algorithm/preproc/` |
| 知识抽取 | `views/component/knowledge-extract/` | `modules/component/knowledge_extract/` | `algorithm/uie/` |
| 知识图谱更新 | `views/component/knowledge-graph-update/` | `modules/component/kg_update/` | `algorithm/KGUpdate/` |
| NL2Cypher | `views/component/nl2cypher/` | `modules/component/nl2cypher/` | `algorithm/NL_to_cypher/` |
| 意图识别 | `views/component/intent-recognition/` | `modules/component/intent_recognition/` | `algorithm/intent_recognition_model/` |
| 答案生成 | `views/component/answer-generation/` | `modules/component/answer_generation/` | `algorithm/ans/` |

## 6. 并行开发原则

- **前端**：负责某组件的开发者只修改 `views/component/{组件id}/` 与 `api/modules/component/{组件id}.js`，通过 manifest 自动注册。
- **后端**：负责某组件的开发者只修改 `modules/component/{组件名}/`，不影响其他组件。
- **算法**：负责某算法的开发者只修改 `algorithm/{模块名}/`，与前后端通过约定接口对接。
- **共享**：`config.py`、`db.py`、`request.js`、`router/index.js` 等为共享核心，修改需团队评审。

# 前端项目结构说明

## 根目录

| 文件/目录       | 说明 |
|----------------|------|
| `src/`         | 源码目录 |
| `public/`      | 静态资源（不经过 Vite 处理） |
| `index.html`   | 入口 HTML |
| `vite.config.js` | Vite 配置（别名 `@` → `src`，API 代理到 `localhost:8000`） |
| `package.json` | 依赖与脚本 |
| `Dockerfile.frontend` / `docker-compose.yml` | 容器化部署 |
| `nginx.conf`   | Nginx 配置（生产） |

## src/ 目录结构

```
src/
├── main.js              # 应用入口：Vue、Pinia、Router、Element Plus
├── App.vue              # 根组件
├── style.css            # 全局样式
├── constants.js         # 常量入口（保证 @/constants 解析），实际定义在 constants/index.js
├── constants/
│   └── index.js         # 公共常量（如 APP_TITLE）
├── router/
│   └── index.js         # 路由定义与导航守卫（标题等）
├── api/
│   ├── index.js         # API 统一导出（使用：import { ... } from '@/api'）
│   ├── request.js       # Axios 封装
│   └── modules/
│       ├── chat.js      # 智能问答
│       ├── knowledge.js # 知识库：文档管理、检索
│       └── component/   # 组件管理相关 API
│           ├── index.js
│           └── knowledge-extract.js
├── utils/
│   └── markdown.js      # Markdown 渲染（marked + highlight + katex）
├── assets/              # 需打包的静态资源
└── views/               # 页面视图
    ├── HomeView.vue     # 首页
    ├── ChatView.vue     # 智能问答
    ├── knowledge/       # 知识库模块
    │   ├── KnowledgeLayout.vue
    │   ├── KnowledgeIndex.vue
    │   ├── KnowledgeQuery.vue
    │   └── DocumentManage.vue
    └── component/       # 组件管理
        ├── layout.vue   # 布局与侧栏
        ├── index.vue    # 组件列表/仪表盘
        ├── config.js    # 组件菜单与路由配置（componentRoutes、componentMenuItems）
        └── knowledge-extract/
            └── index.vue # 知识抽取组件页
```

## 约定与扩展

- **路由**：在 `router/index.js` 中注册；页面标题通过 `meta.title` 与 `APP_TITLE` 拼接。
- **API**：新接口在 `api/modules/` 对应模块中实现，并在 `api/index.js` 中导出。
- **组件管理**：新增可点击组件时需：
  1. 在 `views/component/config.js` 的 `componentMenuItems` 中增加一项并设置 `route`；
  2. 在 `componentRoutes` 中增加子路由；
  3. 在 `views/component/{route}/index.vue` 写页面；
  4. 在 `api/modules/component/` 下新增 `{route}.js` 并在 `api/modules/component/index.js` 中导出。

## 别名

- `@` → `src`（在 `vite.config.js` 中配置），用于 `import xxx from '@/api'`、`@/constants`、`@/views/...` 等。

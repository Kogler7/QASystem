# 贡献指南

本文档说明如何新增组件、修改现有模块以及应遵守的规范。

## 1. 新增组件

### 1.1 前端

1. 在 `frontend/src/views/component/` 下创建目录 `{组件id}/`（如 `my-component/`）。
2. 添加 `manifest.js`：

```javascript
import { SomeIcon } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'my-component',
  title: '我的组件',
  desc: '组件描述',
  icon: SomeIcon,
  color: 'var(--primary-500)',
  route: 'my-component'
}

export const route = {
  path: 'my-component',
  name: 'my-component',
  component: () => import('./index.vue'),
  meta: { title: '我的组件' }
}
```

3. 添加 `index.vue` 页面。
4. 在 `frontend/src/api/modules/component/` 下创建 `my-component.js` 封装 API，并在 `index.js` 中导出。

无需修改 `config.js`，其会通过 `import.meta.glob` 自动收集 manifest。

### 1.2 后端

1. 在 `backend/app/modules/component/` 下创建目录 `my_component/`。
2. 添加 `__init__.py`：

```python
from .router import router
```

3. 添加 `router.py`：定义 FastAPI `APIRouter`，prefix 为 `/api/component/my-component`。
4. 添加 `service.py`：封装对 `algorithm/` 的调用逻辑。
5. 在 `backend/app/modules/component/__init__.py` 中：
   - 添加 `from . import my_component`
   - 在 `COMPONENT_ROUTERS` 列表中加入 `my_component.router`。

### 1.3 算法（如需要）

在 `algorithm/` 下新建模块目录，提供可被 `service.py` 调用的接口（函数或类）。详见各算法模块的 README。

## 2. 修改现有组件

- **前端**：只修改 `views/component/{组件id}/` 和 `api/modules/component/{组件id}.js`。
- **后端**：只修改 `modules/component/{组件名}/`。
- **算法**：只修改 `algorithm/{模块名}/`。

避免修改其他组件的文件，以减少合并冲突。

## 3. 共享文件修改

以下文件为共享核心，修改需谨慎并尽量经过团队评审：

- `backend/app/main.py`、`config.py`、`db.py`
- `frontend/src/router/index.js`、`api/request.js`
- `docs/` 下的文档

## 4. 命名规范

- 前端：`kebab-case`（如 `document-preproc`）
- 后端：`snake_case`（如 `document_preproc`）
- API 路径：`/api/component/{kebab-case}`

## 5. 依赖与配置

- 后端：`backend/requirements.txt` 放置通用依赖；算法专属依赖见各算法目录。
- 前端：`frontend/package.json`
- 环境变量：业务配置以 **`backend/.env`** 为主；各算法目录下的 `.env` 仅用于单独跑脚本（见根目录 `README.md` 与对应 README）

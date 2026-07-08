# develop 分支合并报告（2026-07-08）

## 1. 合并目标

将以下两个远端分支合并进 `develop`：

- `origin/ws-dev`：提交 `b6ebd6e 接口2.0修改`
- `origin/cly-dev`：提交 `3654ee6 feat: 集成文档解析到知识沉淀接口`

合并前确认：

- 当前分支：`develop`
- 工作区：干净
- `origin/ws-dev` 是 `origin/cly-dev` 的祖先，因此按 `ws-dev` → `cly-dev` 的顺序合并。

## 2. 合并结果

本次合并后 `develop` 新增以下提交：

- `1d240e2 Merge origin/ws-dev into develop`
- `f0b60b9 Merge origin/cly-dev into develop`
- `8ecda7e fix: avoid duplicate knowledge precipitation route`

合并后相对 `origin/develop` 的主要变更集中在：

- `algorithm/NL_to_cypher/vector.py`
- `backend/app/modules/component/nl2cypher/router.py`
- `backend/app/modules/component/nl2cypher/service.py`
- `backend/app/routers/knowledge.py`
- `backend/app/services/knowledge_pipeline.py`
- `docs/MERGE_REPORT_20260708.md`

## 3. 冲突处理

### 3.1 `origin/ws-dev` 合并冲突

冲突文件：

- `backend/app/routers/knowledge.py`

冲突原因：

- `develop` 版本保留了知识库文档管理路由的鉴权依赖：
  - `get_session`
  - `require_admin`
  - `Document`
- `origin/ws-dev` 在同一文件中新增了知识沉淀入口所需的 `knowledge_extract_service` 导入和 `/precipitation` 路由。

解决方式：

- 保留 `develop` 的文档管理、数据库会话和管理员鉴权逻辑。
- 合入 `origin/ws-dev` 的代码后发现 `/api/knowledge/precipitation` 与既有协议路由重复注册。
- 最终删除 `backend/app/routers/knowledge.py` 中重复的 `/precipitation` 路由，避免被 `backend/app/routers/knowledge_precipitation.py` 中已验证的协议接口遮挡或产生行为不确定。

### 3.2 `origin/cly-dev` 合并冲突

`origin/cly-dev` 合并无 Git 冲突。

主要变更：

- 增强 `backend/app/services/knowledge_pipeline.py` 中的文档解析/知识沉淀流程。
- 该分支只比 `origin/ws-dev` 多一个提交，因此合并范围集中在服务层。

## 4. 路由去重说明

合并后曾出现两个相同接口路径：

- `backend/app/routers/knowledge_precipitation.py`
  - 暴露 `/api/knowledge/precipitation`
  - 来源：此前已实现并已验证的知识地图算法协议接口。
- `backend/app/routers/knowledge.py`
  - 也新增了 `/api/knowledge/precipitation`
  - 来源：`origin/ws-dev` 的接口 2.0 改动。

由于 `backend/app/main.py` 中先注册 `knowledge_precipitation.router`，再注册 `knowledge.router`，重复路径会导致后注册的实现不可预期或实际不可达。为保持协议接口稳定，本次处理为：

- 保留 `knowledge_precipitation.router` 中的协议入口。
- 删除 `knowledge.py` 中重复的 `/precipitation` 路由。
- 保留 `knowledge_pipeline.py` 中合入的服务层能力，后续如需替换协议实现，可在单一路由中显式切换。

容器验证结果：

- FastAPI app 可导入。
- `/api/knowledge/precipitation` 注册数量为 `1`。

## 5. 验证记录

已执行：

```powershell
python -m py_compile algorithm\NL_to_cypher\vector.py `
  backend\app\modules\component\nl2cypher\router.py `
  backend\app\modules\component\nl2cypher\service.py `
  backend\app\routers\knowledge.py `
  backend\app\services\knowledge_pipeline.py `
  backend\app\routers\knowledge_precipitation.py `
  backend\app\main.py
```

结果：通过。

本机 Python 环境未安装 `fastapi`，因此使用已构建的后端镜像挂载当前代码验证：

```powershell
docker run --rm -v "${PWD}:/app" -w /app qasystem-backend:latest `
  python -c "import sys; sys.path.insert(0, 'backend'); from app.main import app; paths=[r.path for r in app.routes if r.path == '/api/knowledge/precipitation']; print('routes', len(app.routes)); print('precipitation_routes', len(paths))"
```

结果：

```text
routes 77
precipitation_routes 1
```

## 6. 风险与后续建议

- `backend/app/services/knowledge_pipeline.py` 中 `knowledge_extract_service` 仍包含模拟 graph 输出，应在后续迭代中替换为真实知识抽取结果后再作为协议主入口。
- 本次没有重新打包 Docker 镜像；当前验证是用既有镜像挂载最新代码完成的导入检查。
- 建议后续补充针对 4 个协议接口的自动化集成测试，尤其是 `/api/knowledge/precipitation` 的路由唯一性、文件解析、LLM 错误返回和成功响应结构。

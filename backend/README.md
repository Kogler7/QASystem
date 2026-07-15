# Backend

后端是 QASystem 的唯一 ASGI 入口，职责限定为：

- 暴露 `/api/v1` 聚合流水线、文档、图谱和向量化 HTTP 接口。
- 历史路径作为 deprecated aliases 保留，并在响应 `info` 中返回迁移目标。
- 暴露 `/health` 健康检查。
- 在 `/mcp` 挂载统一 MCP Streamable HTTP 服务。

流程编排位于 `backend/app/services/`，算法实现位于 `algorithm/`，MCP 协议适配位于 `mcp_services/`。后端不再包含
旧前端、用户系统、历史编排平台、文档管理或重复组件路由。

## 运行

本地直接启动时，后端会在尚未注入 `ENVIRONMENT` 的情况下加载仓库根目录 `.env`。
Docker、Kubernetes 或其他部署环境应直接注入进程环境变量，并设置
`QASYSTEM_LOAD_DOTENV=false`。进程环境变量的优先级始终高于 dotenv。

```powershell
$env:PYTHONPATH="$PWD;$PWD\agent"
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.test.txt
.\.venv\Scripts\python.exe -m pytest -c backend\pytest.ini backend\tests -q
```

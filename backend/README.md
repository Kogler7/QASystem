# Backend

后端是 QASystem 的唯一 ASGI 入口，职责限定为：

- 暴露知识沉淀、批量向量化和图谱置信度三个 HTTP 接口。
- 暴露 `/health` 健康检查。
- 在 `/mcp` 挂载统一 MCP Streamable HTTP 服务。

流程编排位于 `backend/app/services/`，算法实现位于 `algorithm/`，MCP 协议适配位于 `mcp_services/`。后端不再包含
旧前端、用户系统、历史编排平台、文档管理或重复组件路由。

## 运行

```powershell
$env:PYTHONPATH="$PWD;$PWD\agentcode"
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.test.txt
.\.venv\Scripts\python.exe -m pytest -c backend\pytest.ini backend\tests -q
```

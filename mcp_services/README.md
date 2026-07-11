# Unified MCP Service

所有 QASystem MCP 工具通过同一个 `FastMCP` 实例注册，并由后端挂载到 `/mcp`。

业务域：

- `knowledge`：知识抽取、知识空间初始化和存储。
- `reasoning`：意图识别、规划、知识空间定位和图谱问答。
- `data`：SQL 查询与数据库元数据。
- `visualization`：图表生成。

工具适配层应保持轻量，业务实现放在 `algorithm/`。禁止在业务目录新增独立 MCP Server。

独立启动统一 MCP Server：

```powershell
$env:PYTHONPATH="$PWD"
.\.venv\Scripts\python.exe -m mcp_services.server
```

默认使用 Streamable HTTP，地址由 `MCP_HOST`、`MCP_PORT` 和 `MCP_TRANSPORT` 控制。

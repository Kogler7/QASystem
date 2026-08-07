# Unified MCP Service

所有 QASystem MCP 工具通过同一个 `FastMCP` 实例注册，并由后端挂载到 `/mcp`。

业务域：

- `knowledge`：知识抽取；知识空间初始化和图谱存储工具名暂时保留，调用时返回外部能力未开通。
- `graph`：知识空间定位、实体链接、Stakeholder、实体属性查询和确定性单跳图谱搜索；数据通过应用团队统一 Cypher API 查询并透传鉴权。
- `data`：SQL 查询与数据库元数据。
- `visualization`：图表生成。

工具适配层应保持轻量，业务实现放在 `algorithm/`。禁止在业务目录新增独立 MCP Server。

图谱工具继续经 Redis 队列执行。请求鉴权由 Backend 捕获并以短期 Redis 引用传给 Worker，不得为透传鉴权改成 inline 执行。SQL 直连是当前唯一数据库兼容例外。

所有工具统一返回：

```json
{"code": 0, "message": "success", "data": {}, "error": null}
```

失败时 `code` 为非零值，`data` 为 `null`，`error.details` 提供可诊断信息。

独立启动统一 MCP Server：

```powershell
$env:PYTHONPATH="$PWD"
.\.venv\Scripts\python.exe -m mcp_services.server
```

默认使用 Streamable HTTP，地址由 `MCP_HOST`、`MCP_PORT` 和 `MCP_TRANSPORT` 控制。

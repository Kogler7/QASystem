# 项目结构

## 运行入口

`backend/app/main.py` 是唯一 ASGI 入口。它只注册三个业务接口、健康检查、产物下载和统一 MCP
挂载点，不负责业务编排。

## 目录职责

```text
backend/
├── app/main.py
├── app/routers/
├── app/services/
├── app/schemas.py
└── tests/

mcp_services/
├── runtime.py
├── server.py
└── domains/
    ├── knowledge/
    ├── reasoning/
    ├── data/
    └── visualization/

algorithm/
├── knowledge_precipitation.py
├── preproc/
├── knowledge_extract/
├── NL_to_cypher/
├── confidence_calculate/
├── ans/
├── intent_recognition_model/
├── data_access/
├── visualization/
└── common/
```

## 边界规则

- HTTP 与 MCP 复用算法业务函数，不复制业务实现。
- 所有 MCP 工具注册到 `mcp_services.runtime.mcp`。
- 算法模块不得自行创建 MCP 或 FastAPI Server。
- 新业务配置统一使用环境变量，不在导入时改写环境变量。
- `agentcode` 只作为 Agent 和 MCP 客户端，不承载服务端业务实现。

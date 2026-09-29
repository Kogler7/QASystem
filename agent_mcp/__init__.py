"""Agent 专属 MCP 服务。

与旧 MCP（backend:8000/mcp，qasystem-tools，服务旧智能体业务流程）完全独立：
本服务只暴露给 agent 使用的通用图谱工具，不 import 任何 mcp_services 代码，
旧 MCP 及其工具零改动。
"""

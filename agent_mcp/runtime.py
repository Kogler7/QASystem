import os

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "agent-tools",
    instructions="知识图谱检索与分析工具集：实体解析、图谱探索、空间定位、只读 Cypher 查询、SQL 执行、图表生成",
    host=os.getenv("AGENT_MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("AGENT_MCP_PORT", "9181")),
)

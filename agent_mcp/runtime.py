from docker.startup_contract import INTERNAL_MCP_BIND, INTERNAL_AGENT_MCP_PORT

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "agent-tools",
    instructions="知识图谱检索与分析工具集：实体解析、图谱探索、空间定位、只读 Cypher 查询、SQL 执行、图表生成",
    host=INTERNAL_MCP_BIND,
    port=INTERNAL_AGENT_MCP_PORT,
)

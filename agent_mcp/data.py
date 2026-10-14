"""SQL 执行工具——原样搬运自旧 MCP data/sql.py（该域唯一活跃工具 execute_sql）。

agent-mcp 容器不挂 mcp_services 卷，不走统一执行队列（maybe_submit），
直接同步调用 algorithm.data_access.sql.SQLExecutor；关系型 SQL 连接串取 DATABASE_URL
（.env 已配置，host.docker.internal 经 extra_hosts 可达）。
"""
from docker.configuration import getenv as config_getenv

import os
from typing import Any

from agent_mcp.runtime import mcp


def _executor(database_url: str | None = None, *, read_only: bool = True):
    from algorithm.data_access.sql import SQLExecutor

    url = database_url or config_getenv("DATABASE_URL", "")
    if not url:
        raise ValueError("未配置关系型 SQL 数据库连接 DATABASE_URL")
    return SQLExecutor(url, read_only=read_only)


@mcp.tool(name="execute_sql")
def execute_sql(
    sql: str,
    params: dict[str, Any] | None = None,
    database_url: str | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    """Execute SQL against the configured database. 返回 {success, data: 行数组, rowcount, columns, error_message, query_type, execution_time_ms}，行是 {列名: 值}；直接按行/表格呈现即可，不要对 data 再做整体 JSON 二次序列化或改写，结果多时只取前若干行并注明 rowcount。"""
    with _executor(database_url, read_only=read_only) as executor:
        return executor.execute(sql, params=params).to_dict()

from docker.configuration import getenv as config_getenv
import os
from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, invoke_tool, tool_error, tool_success
from mcp_services.dispatch import maybe_submit


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
    """Execute SQL against the configured database. 返回的 data 为 {success, data: 行数组, rowcount, columns, ...}，行是 {列名: 值}；直接按行/表格呈现即可，不要对 data 再做整体 JSON 二次序列化或改写，结果多时只取前若干行并注明 rowcount。"""
    queued = maybe_submit(
        "mcp.execute_sql",
        {
            "sql": sql,
            "params": params,
            "database_url": database_url,
            "read_only": read_only,
        },
        resource_class="io.db",
    )
    if queued is not None:
        return queued
    return invoke_tool(
        lambda: _execute_sql(sql, params, database_url, read_only),
        ToolCode.DATA_FAILED,
        "SQL 执行",
    )


def _execute_sql(sql, params, database_url, read_only):
    with _executor(database_url, read_only=read_only) as executor:
        return executor.execute(sql, params=params).to_dict()


# 项目要求所有信息来源于知识图谱，不再允许 Agent 直接查询数据库表结构/字段等信息，
# 以免在数据库中反复查找信息。以下工具已停用，仅保留 execute_sql 作为 SQL 执行入口。
# 若后续需要恢复，取消注释即可。
#
# @mcp.tool(name="list_tables")
# def list_tables(database_url: str | None = None) -> dict[str, Any]:
#     """List tables in the configured database."""
#     return invoke_tool(lambda: _list_tables(database_url), ToolCode.DATA_FAILED, "数据表查询")
#
#
# def _list_tables(database_url: str | None = None) -> dict[str, Any]:
#     with _executor(database_url) as executor:
#         dialect = executor.dialect.value
#         if dialect in {"mysql", "mariadb"}:
#             sql = "SHOW TABLES"
#         elif dialect == "sqlite":
#             sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
#         else:
#             sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
#         result = executor.execute(sql).to_dict()
#         result["dialect"] = dialect
#         return result
#
#
# @mcp.tool(name="describe_table")
# def describe_table(table_name: str, database_url: str | None = None) -> dict[str, Any]:
#     """Describe columns in a database table."""
#     if not table_name.replace("_", "").isalnum():
#         return tool_error(ToolCode.INVALID_ARGUMENT, "table_name 只能包含字母、数字和下划线")
#     return invoke_tool(lambda: _describe_table(table_name, database_url), ToolCode.DATA_FAILED, "表结构查询")
#
#
# def _describe_table(table_name: str, database_url: str | None = None) -> dict[str, Any]:
#     with _executor(database_url) as executor:
#         dialect = executor.dialect.value
#         if dialect in {"mysql", "mariadb"}:
#             sql = f"DESCRIBE `{table_name}`"
#         elif dialect == "sqlite":
#             sql = f"PRAGMA table_info('{table_name}')"
#         else:
#             sql = (
#                 "SELECT column_name, data_type, is_nullable "
#                 "FROM information_schema.columns "
#                 f"WHERE table_name = '{table_name}' ORDER BY ordinal_position"
#             )
#         result = executor.execute(sql).to_dict()
#         result["dialect"] = dialect
#         return result
#
#
# @mcp.tool(name="list_supported_dialects")
# def list_supported_dialects() -> dict[str, Any]:
#     """List database dialects supported by the SQL tool."""
#     return tool_success(["mysql", "mariadb", "postgresql", "sqlite", "mssql", "oracle"], "数据库方言查询完成")
#
#
# @mcp.tool(name="get_database_info")
# def get_database_info(database_url: str | None = None) -> dict[str, Any]:
#     """Return configured database dialect and connectivity status."""
#     return invoke_tool(lambda: _get_database_info(database_url), ToolCode.DATA_FAILED, "数据库连接信息查询")
#
#
# def _get_database_info(database_url: str | None = None) -> dict[str, Any]:
#     with _executor(database_url) as executor:
#         return {
#             "dialect": executor.dialect.value,
#             "read_only": executor.read_only,
#             "connected": executor.test_connection(),
#         }

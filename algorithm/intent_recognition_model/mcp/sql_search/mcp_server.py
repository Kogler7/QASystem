from fastmcp.server import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from .executor import SQLExecutor, SQLResult, _detect_dialect
import os

app = FastMCP(
    name="SQL Search MCP",
    version="1.0.0"
)

# 预设数据库配置（从环境变量或配置文件读取）
DEFAULT_DATABASE_URL = os.getenv(
    "DEFAULT_DATABASE_URL",
    "mysql+pymysql://rail_user:123456@127.0.0.1:3306/rail_assistant"
)


class ExecuteSQLRequest(BaseModel):
    sql: str = Field(..., description="The SQL statement to execute")
    database_url: Optional[str] = Field(
        None,
        description="Database connection URL. If not provided, uses default database. "
                    "Format: mysql+pymysql://user:pass@host:3306/db"
    )
    read_only: Optional[bool] = Field(True, description="If True, only SELECT queries are allowed")
    max_row_limit: Optional[int] = Field(10000, description="Maximum number of rows to return (1-10000)")


class ExecuteSQLResponse(BaseModel):
    success: bool = Field(..., description="Whether the query executed successfully")
    data: List[Dict[str, Any]] = Field(..., description="Query result data (empty for non-SELECT queries)")
    rowcount: int = Field(..., description="Number of rows affected or returned")
    columns: List[str] = Field(..., description="Column names for SELECT queries")
    error_message: str = Field(..., description="Error message if failed")
    query_type: str = Field(..., description="Type of query: select, insert, update, delete, ddl")
    execution_time_ms: float = Field(..., description="Execution time in milliseconds")
    database_used: str = Field(..., description="The database URL used (password masked)")


def _normalize_request(data: Union[Dict, BaseModel], model_class) -> BaseModel:
    """兼容 Claude Code 和 FastMCP 的参数格式"""
    if isinstance(data, BaseModel):
        return data
    # 处理 Claude Code 直接传递参数的格式
    if isinstance(data, dict):
        # 如果已经是嵌套格式，直接解析
        if "request" in data and isinstance(data["request"], dict):
            return model_class(**data["request"])
        # 否则直接用 dict 解析
        return model_class(**data)
    return model_class()


@app.tool(
    name="execute_sql",
    description="Execute a SQL query. If no database URL is provided, uses the default database.",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def execute_sql(request: Union[ExecuteSQLRequest, Dict[str, Any]]) -> ExecuteSQLResponse:
    try:
        # 兼容 Claude Code 格式
        req = _normalize_request(request, ExecuteSQLRequest)
        
        # 使用默认数据库 URL（如果未提供）
        db_url = req.database_url or DEFAULT_DATABASE_URL
        
        executor = SQLExecutor(
            database_url=db_url,
            read_only=req.read_only or True,
            max_row_limit=min(max(1, req.max_row_limit or 10000), 10000)
        )
        
        result = executor.execute(req.sql)
        executor.close()
        
        # 脱敏处理
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return ExecuteSQLResponse(
            success=result.success,
            data=result.data,
            rowcount=result.rowcount,
            columns=result.columns,
            error_message=result.error_message,
            query_type=result.query_type.value,
            execution_time_ms=result.execution_time_ms,
            database_used=masked_url
        )
    except Exception as e:
        db_url = ""
        if isinstance(request, (ExecuteSQLRequest, dict)):
            db_url = getattr(request, 'database_url', request.get('database_url', '')) if isinstance(request, dict) else getattr(request, 'database_url', '')
        db_url = db_url or DEFAULT_DATABASE_URL
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return ExecuteSQLResponse(
            success=False,
            data=[],
            rowcount=0,
            columns=[],
            error_message=f"Unexpected error: {str(e)}",
            query_type="unknown",
            execution_time_ms=0.0,
            database_used=masked_url
        )


class ListTablesRequest(BaseModel):
    database_url: Optional[str] = Field(
        None,
        description="Database connection URL. If not provided, uses default database."
    )


class ListTablesResponse(BaseModel):
    success: bool = Field(..., description="Whether the query executed successfully")
    tables: List[str] = Field(..., description="List of table names")
    database_used: str = Field(..., description="The database URL used (password masked)")
    error_message: str = Field(..., description="Error message if failed")


@app.tool(
    name="list_tables",
    description="List all tables in the database. If no database URL is provided, uses the default database.",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def list_tables(request: Union[ListTablesRequest, Dict[str, Any], None] = None) -> ListTablesResponse:
    try:
        # 兼容 Claude Code 格式
        req = _normalize_request(request or {}, ListTablesRequest)
        
        db_url = req.database_url or DEFAULT_DATABASE_URL
        
        executor = SQLExecutor(database_url=db_url, read_only=True)
        result = executor.execute("SHOW TABLES")
        executor.close()
        
        tables = [list(row.values())[0] for row in result.data] if result.success else []
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return ListTablesResponse(
            success=result.success,
            tables=tables,
            database_used=masked_url,
            error_message=result.error_message
        )
    except Exception as e:
        db_url = ""
        if isinstance(request, (ListTablesRequest, dict)):
            db_url = getattr(request, 'database_url', request.get('database_url', '')) if isinstance(request, dict) else getattr(request, 'database_url', '')
        db_url = db_url or DEFAULT_DATABASE_URL
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return ListTablesResponse(
            success=False,
            tables=[],
            database_used=masked_url,
            error_message=str(e)
        )


class DescribeTableRequest(BaseModel):
    table_name: str = Field(..., description="Name of the table to describe")
    database_url: Optional[str] = Field(
        None,
        description="Database connection URL. If not provided, uses default database."
    )


class DescribeTableResponse(BaseModel):
    success: bool = Field(..., description="Whether the query executed successfully")
    table_name: str = Field(..., description="The table name")
    columns: List[Dict[str, Any]] = Field(..., description="Column information")
    database_used: str = Field(..., description="The database URL used (password masked)")
    error_message: str = Field(..., description="Error message if failed")


@app.tool(
    name="describe_table",
    description="Describe a table structure (columns, types, etc.). If no database URL is provided, uses the default database.",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def describe_table(request: Union[DescribeTableRequest, Dict[str, Any]]) -> DescribeTableResponse:
    try:
        # 兼容 Claude Code 格式
        req = _normalize_request(request, DescribeTableRequest)
        
        db_url = req.database_url or DEFAULT_DATABASE_URL
        
        executor = SQLExecutor(database_url=db_url, read_only=True)
        result = executor.execute(f"DESCRIBE {req.table_name}")
        executor.close()
        
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return DescribeTableResponse(
            success=result.success,
            table_name=req.table_name,
            columns=result.data,
            database_used=masked_url,
            error_message=result.error_message
        )
    except Exception as e:
        db_url = ""
        table_name = ""
        if isinstance(request, (DescribeTableRequest, dict)):
            db_url = getattr(request, 'database_url', request.get('database_url', '')) if isinstance(request, dict) else getattr(request, 'database_url', '')
            table_name = getattr(request, 'table_name', request.get('table_name', '')) if isinstance(request, dict) else getattr(request, 'table_name', '')
        db_url = db_url or DEFAULT_DATABASE_URL
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return DescribeTableResponse(
            success=False,
            table_name=table_name,
            columns=[],
            database_used=masked_url,
            error_message=str(e)
        )


class ListSupportedDialectsResponse(BaseModel):
    dialects: List[str] = Field(..., description="List of supported database dialects")
    description: Dict[str, str] = Field(..., description="Description of each dialect")


@app.tool(
    name="list_supported_dialects",
    description="List all supported database dialects and their connection URL formats",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def list_supported_dialects() -> ListSupportedDialectsResponse:
    dialect_info = {
        "mysql": "MySQL - Format: mysql+pymysql://user:pass@host:3306/db",
        "postgresql": "PostgreSQL - Format: postgresql://user:pass@host:5432/db",
        "sqlite": "SQLite - Format: sqlite:///path/to/database.db",
        "mariadb": "MariaDB - Format: mariadb+pymysql://user:pass@host:3306/db",
        "mssql": "Microsoft SQL Server - Format: mssql+pymssql://user:pass@host:1433/db",
        "oracle": "Oracle - Format: oracle+cx_oracle://user:pass@host:1521/service"
    }
    
    return ListSupportedDialectsResponse(
        dialects=list(dialect_info.keys()),
        description=dialect_info
    )


class GetDatabaseInfoResponse(BaseModel):
    success: bool = Field(..., description="Whether the query executed successfully")
    database_name: str = Field(..., description="Current database name")
    table_count: int = Field(..., description="Number of tables")
    tables: List[str] = Field(..., description="List of table names")
    database_used: str = Field(..., description="The database URL used (password masked)")


@app.tool(
    name="get_database_info",
    description="Get information about the default database (or specified database)",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def get_database_info(request: Union[ListTablesRequest, Dict[str, Any], None] = None) -> GetDatabaseInfoResponse:
    try:
        # 兼容 Claude Code 格式
        req = _normalize_request(request or {}, ListTablesRequest)
        
        db_url = req.database_url if req else DEFAULT_DATABASE_URL
        
        executor = SQLExecutor(database_url=db_url, read_only=True)
        
        # 获取数据库名称
        db_name_result = executor.execute("SELECT DATABASE() as db_name")
        db_name = db_name_result.data[0]["db_name"] if db_name_result.success and db_name_result.data else "unknown"
        
        # 获取表列表
        tables_result = executor.execute("SHOW TABLES")
        tables = [list(row.values())[0] for row in tables_result.data] if tables_result.success else []
        
        executor.close()
        
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return GetDatabaseInfoResponse(
            success=True,
            database_name=db_name,
            table_count=len(tables),
            tables=tables,
            database_used=masked_url
        )
    except Exception as e:
        db_url = ""
        if isinstance(request, (ListTablesRequest, dict)):
            db_url = getattr(request, 'database_url', request.get('database_url', '')) if isinstance(request, dict) else getattr(request, 'database_url', '')
        db_url = db_url or DEFAULT_DATABASE_URL
        masked_url = db_url.replace("://", "://***:***@") if "://" in db_url else db_url
        
        return GetDatabaseInfoResponse(
            success=False,
            database_name="",
            table_count=0,
            tables=[],
            database_used=masked_url
        )


if __name__ == "__main__":
    import sys
    import asyncio
    
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        print("🟢 启动 SQL Search MCP stdio 服务...")
        print(f"📋 默认数据库: {DEFAULT_DATABASE_URL}")
        asyncio.run(app.run_stdio_async())
    else:
        print("🟢 启动 SQL Search MCP HTTP 服务...")
        print(f"📡 服务地址: http://0.0.0.0:8889")
        print(f"📋 默认数据库: {DEFAULT_DATABASE_URL}")
        print(f"🔧 使用方式:")
        print(f"   - OpenAPI 文档: http://localhost:8889/docs")
        print(f"   - 启动 stdio 模式: {sys.argv[0]} stdio")
        asyncio.run(app.run_http_async(host="0.0.0.0", port=8889))

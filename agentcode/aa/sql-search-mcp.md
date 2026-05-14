# SQL Search MCP — 工具清单与实现说明

## 一、服务概览

| 属性 | 值 |
|------|-----|
| 服务名 | SQL Search MCP |
| 版本 | 1.0.0 |
| 框架 | FastMCP |
| 传输协议 | HTTP（JSON-RPC）或 stdio |
| 默认端口 | `8889`（HTTP 模式） |
| 默认数据库 | `mysql+pymysql://rail_user@127.0.0.1:3306/rail_assistant`（可通过 `DEFAULT_DATABASE_URL` 环境变量覆盖） |
| 源码路径 | `algorithm/intent_recognition_model/mcp/sql_search/mcp_server.py` |
| 执行器路径 | `algorithm/intent_recognition_model/mcp/sql_search/executor.py` |
| 核心依赖 | SQLAlchemy + PyMySQL |
| 支持方言 | MySQL、PostgreSQL、SQLite、MariaDB、MSSQL、Oracle |

**启动方式**：

```
# HTTP 模式（默认）
python -m algorithm.intent_recognition_model.mcp.sql_search.mcp_server
# → 启动 HTTP 服务在 0.0.0.0:8889

# stdio 模式
python -m algorithm.intent_recognition_model.mcp.sql_search.mcp_server stdio
```
启动后可通过 `http://localhost:8889/docs` 查看 OpenAPI 文档。

---

## 二、工具清单

### 2.1 `execute_sql` — 执行 SQL 查询

**功能**：执行任意 SQL 语句，支持 SELECT / INSERT / UPDATE / DELETE / DDL。默认只读模式仅允许查询类语句。

**参数**（`ExecuteSQLRequest`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `sql` | `str` | ✅ | — | 要执行的 SQL 语句 |
| `database_url` | `str` | 否 | `None` | 数据库连接 URL，不提供则使用默认数据库。格式：`mysql+pymysql://user:pass@host:3306/db` |
| `read_only` | `bool` | 否 | `True` | 为 `True` 时只允许 SELECT / SHOW / DESCRIBE / EXPLAIN / WITH 查询 |
| `max_row_limit` | `int` | 否 | `10000` | 最大返回行数（1–10000） |

**返回**（`ExecuteSQLResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 是否执行成功 |
| `data` | `list[dict]` | 查询结果数据（非 SELECT 时为空数组） |
| `rowcount` | `int` | 影响或返回的行数 |
| `columns` | `list[str]` | 列名列表 |
| `error_message` | `str` | 错误信息（成功时为空） |
| `query_type` | `str` | 查询类型：`select` / `insert` / `update` / `delete` / `ddl` / `unknown` |
| `execution_time_ms` | `float` | 执行耗时（毫秒） |
| `database_used` | `str` | 所用数据库 URL（密码已脱敏，如 `mysql+pymysql://***:***@host:3306/db`） |

**注解**：`readOnlyHint: True, idempotentHint: True, destructiveHint: False`

---

### 2.2 `list_tables` — 列出所有表

**功能**：列出当前数据库的所有表名（执行 `SHOW TABLES`）。

**参数**（`ListTablesRequest`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `database_url` | `str` | 否 | `None` | 数据库连接 URL，不提供则使用默认数据库 |

**返回**（`ListTablesResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 是否成功 |
| `tables` | `list[str]` | 表名列表 |
| `database_used` | `str` | 所用数据库 URL（密码已脱敏） |
| `error_message` | `str` | 错误信息 |

**注解**：同 2.1

---

### 2.3 `describe_table` — 查看表结构

**功能**：查看指定表的列名、类型、键、默认值等结构信息（执行 `DESCRIBE <table_name>`）。

**参数**（`DescribeTableRequest`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `table_name` | `str` | ✅ | — | 要查看的表名 |
| `database_url` | `str` | 否 | `None` | 数据库连接 URL |

**返回**（`DescribeTableResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 是否成功 |
| `table_name` | `str` | 表名 |
| `columns` | `list[dict]` | 列信息列表，每项包含 `Field`、`Type`、`Null`、`Key`、`Default`、`Extra` 等字段 |
| `database_used` | `str` | 所用数据库 URL（密码已脱敏） |
| `error_message` | `str` | 错误信息 |

**注解**：同 2.1

---

### 2.4 `get_database_info` — 获取数据库总览

**功能**：获取数据库名称、表数量及完整表列表。组合了 `SELECT DATABASE()` + `SHOW TABLES`。

**参数**（可选 `ListTablesRequest`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `database_url` | `str` | 否 | `None` | 数据库连接 URL（参数整体可为 `None`） |

**返回**（`GetDatabaseInfoResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 是否成功 |
| `database_name` | `str` | 当前数据库名（通过 `SELECT DATABASE()` 获取） |
| `table_count` | `int` | 表数量 |
| `tables` | `list[str]` | 表名列表 |
| `database_used` | `str` | 所用数据库 URL（密码已脱敏） |

**注解**：同 2.1

---

### 2.5 `list_supported_dialects` — 列出支持的数据库方言

**功能**：返回此 MCP 服务支持的所有数据库方言及对应的连接 URL 格式，无需连接数据库。

**参数**：无

**返回**（`ListSupportedDialectsResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `dialects` | `list[str]` | 方言列表：`mysql`、`postgresql`、`sqlite`、`mariadb`、`mssql`、`oracle` |
| `description` | `dict[str, str]` | 每种方言的连接 URL 格式说明 |

**返回示例**：

```json
{
  "dialects": ["mysql", "postgresql", "sqlite", "mariadb", "mssql", "oracle"],
  "description": {
    "mysql": "MySQL - Format: mysql+pymysql://user:pass@host:3306/db",
    "postgresql": "PostgreSQL - Format: postgresql://user:pass@host:5432/db",
    "sqlite": "SQLite - Format: sqlite:///path/to/database.db",
    "mariadb": "MariaDB - Format: mariadb+pymysql://user:pass@host:3306/db",
    "mssql": "Microsoft SQL Server - Format: mssql+pymssql://user:pass@host:1433/db",
    "oracle": "Oracle - Format: oracle+cx_oracle://user:pass@host:1521/service"
  }
}
```

**注解**：同 2.1

---

## 三、实现说明

### 3.1 整体架构

```
MCP Client (AgentRuntime)
    │ JSON-RPC over HTTP
    ▼
FastMCP HTTP Server (mcp_server.py)
    │ @app.tool 装饰器注册 5 个工具
    ├─ execute_sql()
    ├─ list_tables()
    ├─ describe_table()
    ├─ get_database_info()
    └─ list_supported_dialects()
    │
    ▼
SQLExecutor (executor.py)
    │ 封装 SQLAlchemy 连接池 + SQL 执行
    ├─ 方言自动检测
    ├─ 只读模式校验
    ├─ SQL 类型分类
    ├─ 行数限制
    ├─ 密码脱敏
    └─ 分层异常捕获
```

### 3.2 核心类：`SQLExecutor`

**构造参数**：

```python
class SQLExecutor:
    def __init__(
        self, database_url: str, *,
        pool_size=5,       # 连接池大小
        max_overflow=10,   # 最大溢出连接数
        pool_timeout=30,   # 连接池超时（秒）
        pool_recycle=3600, # 连接回收时间（秒）
        connect_timeout=10,# 连接超时（秒）
        query_timeout=60,  # 查询超时（秒）
        read_only=False,   # 只读模式
        max_row_limit=10000 # 最大行数限制
    )
```

**核心方法**：

```python
def execute(self, sql, params=None) -> SQLResult:
    query_type = _classify_query(sql)     # 1. 分类 SQL 类型
    self._validate_sql(sql)               # 2. 只读模式检查
    with self._connection() as conn:      # 3. 从连接池取连接
        result = conn.execute(text(sql))  # 4. 执行 SQL
        if SELECT:
            data = result.fetchall()     # 5. 读取数据（截断到 max_row_limit）
        conn.commit()
    return SQLResult(success=True, ...)
```

### 3.3 SQL 类型分类

```python
# 只读模式识别
_READ_ONLY_PATTERNS = re.compile(r"^\s*(SELECT|SHOW|DESCRIBE|EXPLAIN|WITH)\b", re.IGNORECASE)

# 写操作分类
_WRITE_PATTERNS = {
    SQLQueryType.INSERT: re.compile(r"^\s*INSERT\b", re.IGNORECASE),
    SQLQueryType.UPDATE: re.compile(r"^\s*UPDATE\b", re.IGNORECASE),
    SQLQueryType.DELETE: re.compile(r"^\s*DELETE\b", re.IGNORECASE),
    SQLQueryType.DDL:    re.compile(r"^\s*(CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE)\b", re.IGNORECASE),
}
```

只读模式下，如果 SQL 以 INSERT/UPDATE/DELETE/DDL 关键字开头，直接抛出 `PermissionError`，在数据库层之前拦截。

### 3.4 方言自动检测

```python
def _detect_dialect(url: str) -> SQLDialect:
    lower = url.lower()
    if lower.startswith("mysql") or "+pymysql" in lower: return SQLDialect.MYSQL
    if lower.startswith("postgresql"):                   return SQLDialect.POSTGRESQL
    if lower.startswith("sqlite"):                       return SQLDialect.SQLITE
    if lower.startswith("mariadb"):                      return SQLDialect.MARIADB
    if lower.startswith("mssql"):                        return SQLDialect.MSSQL
    if lower.startswith("oracle"):                       return SQLDialect.ORACLE
    return SQLDialect.UNKNOWN
```

根据连接 URL 的前缀自动识别数据库类型，不同方言有不同的连接超时参数：
- MySQL/MariaDB：使用 `connect_timeout`
- PostgreSQL：使用 `connect_timeout`
- MSSQL：使用 `timeout`

### 3.5 连接池管理

```python
def _build_engine(self) -> Engine:
    return create_engine(
        self._database_url,
        poolclass=QueuePool,       # 队列连接池
        pool_size=self._pool_size, # 默认 5
        max_overflow=10,           # 最大溢出 10
        pool_pre_ping=True,        # 使用前 ping 检测连接存活
        pool_recycle=3600,         # 1 小时后回收连接
    )
```

**关键设计**：
- `pool_pre_ping=True`：每次从池中取连接前先发 `SELECT 1` 验证，防止使用已断开的连接（MySQL 默认 8 小时超时会断连）
- 延迟创建：`_engine` 第一次被访问时才创建（`_get_engine` 懒加载）
- 支持上下文管理器：`with SQLExecutor(...) as executor:` 自动管理生命周期

### 3.6 错误处理层次

```
SQLExecutor.execute() 内部按异常类型分层捕获：
  Level 1: ValueError       — 参数校验失败（空 SQL）
  Level 2: PermissionError  — 只读模式拒绝写操作
  Level 3: ProgrammingError — SQL 语法错误
  Level 4: OperationalError — 数据库连接/操作错误
  Level 5: InterfaceError   — 数据库接口错误
  Level 6: DBAPIError       — 数据库驱动层错误
  Level 7: ArgumentError    — SQLAlchemy 参数错误
  Level 8: SQLAlchemyError  — 其他 SQLAlchemy 异常兜底
  Level 9: Exception        — 未预期异常兜底
```

每层都返回结构化 `SQLResult(success=False, error_message=...)` 而非抛出异常，确保 MCP 调用方始终拿到合法 JSON。

### 3.7 Claude Code 参数格式兼容

```python
def _normalize_request(data, model_class):
    """
    兼容两种调用格式：
    1. FastMCP 原生格式：直接传 Pydantic 模型
    2. Claude Code 格式：  {"request": {...}} 嵌套
    """
    if isinstance(data, BaseModel): return data
    if isinstance(data, dict):
        if "request" in data: return model_class(**data["request"])
        return model_class(**data)
```

Claude Code 调用 MCP 工具时有时会在参数外面包一层 `{"request": {...}}`，这个函数统一解包。

### 3.8 密码脱敏

```python
def _sanitize_url(url: str) -> str:
    # mysql+pymysql://rail_user:123456@127.0.0.1:3306/db
    # → mysql+pymysql://rail_user:****@127.0.0.1:3306/db
    prefix, rest = url.rsplit("@", 1)
    user_part, password_part = prefix.rsplit(":", 1)
    return f"{user_part}:****@{rest}"
```

在 `database_used` 返回字段中对密码做脱敏，防止通过 MCP 返回泄露数据库凭证。

---

## 四、典型调用流程

```
Agent LLM 输出 tool_use:
  {
    "name": "sql-search__execute_sql",
    "input": {
      "request": {
        "sql": "SELECT COUNT(*) FROM ads_dzcg_bidsum",
        "read_only": true
      }
    }
  }
    │
    ▼
ToolManager.call_tool("sql-search__execute_sql", {...})
    │ JSON-RPC over HTTP
    ▼
execute_sql(request={"sql": "SELECT COUNT(*)...", "read_only": True})
    │ _normalize_request() 解包 Claude Code 格式
    ▼
SQLExecutor(database_url=..., read_only=True).execute(sql)
    │
    ├─ _classify_query("SELECT COUNT(*)") → SQLQueryType.SELECT
    ├─ _validate_sql() → 通过（SELECT 允许）
    ├─ _connection() → 从连接池取连接
    ├─ conn.execute(text("SELECT COUNT(*)..."))
    ├─ _format_result() → 截断到 max_row_limit 行
    ├─ conn.commit()
    ├─ 计时 → execution_time_ms
    │
    ▼
ExecuteSQLResponse(
    success=True,
    data=[{"COUNT(*)": 127}],
    rowcount=127,
    columns=["COUNT(*)"],
    query_type="select",
    execution_time_ms=7.4,
    database_used="mysql+pymysql://***:***@127.0.0.1:3306/rail_assistant"
)
```

---

## 五、安全机制汇总

| 机制 | 实现方式 |
|------|---------|
| **只读模式** | 正则匹配 SQL 前缀，拒绝 INSERT/UPDATE/DELETE/DDL |
| **行数限制** | `max_row_limit` 参数（1–10000），超过截断 |
| **密码脱敏** | `database_used` 返回字段中密码替换为 `****` |
| **查询超时** | `query_timeout` 参数（默认 60s） |
| **连接池 PING** | `pool_pre_ping=True` 防止使用已断开连接 |
| **连接回收** | `pool_recycle=3600` 1 小时后强制回收连接 |
| **异常兜底** | 6 层异常捕获，始终返回结构化错误 |

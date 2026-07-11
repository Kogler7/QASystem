from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generator, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, Result
from sqlalchemy.exc import (
    ArgumentError,
    DBAPIError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


class SQLDialect(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    MARIADB = "mariadb"
    MSSQL = "mssql"
    ORACLE = "oracle"
    UNKNOWN = "unknown"


class SQLQueryType(str, Enum):
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    DDL = "ddl"
    UNKNOWN = "unknown"


@dataclass
class SQLResult:
    success: bool
    data: list[dict[str, Any]] = field(default_factory=list)
    rowcount: int = 0
    columns: list[str] = field(default_factory=list)
    error_message: str = ""
    query_type: SQLQueryType = SQLQueryType.UNKNOWN
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "rowcount": self.rowcount,
            "columns": self.columns,
            "error_message": self.error_message,
            "query_type": self.query_type.value,
            "execution_time_ms": round(self.execution_time_ms, 2),
        }


_READ_ONLY_PATTERNS = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|EXPLAIN|WITH)\b", re.IGNORECASE | re.DOTALL
)

_WRITE_PATTERNS = {
    SQLQueryType.INSERT: re.compile(r"^\s*INSERT\b", re.IGNORECASE),
    SQLQueryType.UPDATE: re.compile(r"^\s*UPDATE\b", re.IGNORECASE),
    SQLQueryType.DELETE: re.compile(r"^\s*DELETE\b", re.IGNORECASE),
    SQLQueryType.DDL: re.compile(
        r"^\s*(CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE)\b", re.IGNORECASE
    ),
}


def _classify_query(sql: str) -> SQLQueryType:
    stripped = sql.strip()
    if not stripped:
        return SQLQueryType.UNKNOWN
    if _READ_ONLY_PATTERNS.match(stripped):
        return SQLQueryType.SELECT
    for qtype, pattern in _WRITE_PATTERNS.items():
        if pattern.match(stripped):
            return qtype
    return SQLQueryType.UNKNOWN


def _detect_dialect(url: str) -> SQLDialect:
    lower = url.lower()
    if lower.startswith("mysql") or "+pymysql" in lower or "+asyncmy" in lower:
        return SQLDialect.MYSQL
    if lower.startswith("postgresql") or lower.startswith("postgres"):
        return SQLDialect.POSTGRESQL
    if lower.startswith("sqlite"):
        return SQLDialect.SQLITE
    if lower.startswith("mariadb"):
        return SQLDialect.MARIADB
    if lower.startswith("mssql") or lower.startswith("pyodbc"):
        return SQLDialect.MSSQL
    if lower.startswith("oracle"):
        return SQLDialect.ORACLE
    return SQLDialect.UNKNOWN


def _sanitize_url(url: str) -> str:
    if "@" not in url:
        return url
    try:
        prefix, rest = url.rsplit("@", 1)
        if ":" in prefix:
            user_part, password_part = prefix.rsplit(":", 1)
            return f"{user_part}:****@{rest}"
    except ValueError:
        pass
    return url


class SQLExecutor:
    _MAX_ROW_LIMIT = 10000
    _DEFAULT_POOL_SIZE = 5
    _DEFAULT_MAX_OVERFLOW = 10
    _DEFAULT_POOL_TIMEOUT = 30
    _CONNECT_TIMEOUT = 10
    _QUERY_TIMEOUT = 60

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: int = 30,
        pool_recycle: int = 3600,
        connect_timeout: int = 10,
        query_timeout: int = 60,
        read_only: bool = False,
        max_row_limit: int = 10000,
    ) -> None:
        if not database_url or not database_url.strip():
            raise ValueError("database_url must be a non-empty string")

        self._database_url = database_url.strip()
        self._read_only = read_only
        self._max_row_limit = min(max(1, max_row_limit), self._MAX_ROW_LIMIT)
        self._query_timeout = query_timeout
        self._dialect = _detect_dialect(self._database_url)
        self._engine: Optional[Engine] = None

        self._pool_size = max(1, pool_size)
        self._max_overflow = max(0, max_overflow)
        self._pool_timeout = max(1, pool_timeout)
        self._pool_recycle = max(60, pool_recycle)
        self._connect_timeout = max(1, connect_timeout)

    @property
    def dialect(self) -> SQLDialect:
        return self._dialect

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _build_engine(self) -> Engine:
        connect_args: dict[str, Any] = {}
        if self._dialect in (SQLDialect.MYSQL, SQLDialect.MARIADB):
            connect_args["connect_timeout"] = self._connect_timeout
        elif self._dialect == SQLDialect.POSTGRESQL:
            connect_args["connect_timeout"] = self._connect_timeout
        elif self._dialect == SQLDialect.MSSQL:
            connect_args["timeout"] = self._connect_timeout

        return create_engine(
            self._database_url,
            poolclass=QueuePool,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
            pool_recycle=self._pool_recycle,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

    def _get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = self._build_engine()
            logger.info(
                "Engine created for dialect=%s, url=%s",
                self._dialect.value,
                _sanitize_url(self._database_url),
            )
        return self._engine

    @contextmanager
    def _connection(self) -> Generator[Connection, None, None]:
        engine = self._get_engine()
        conn: Optional[Connection] = None
        try:
            conn = engine.connect()
            yield conn
        except InterfaceError as exc:
            logger.error("Connection interface error: %s", exc)
            raise
        except OperationalError as exc:
            logger.error("Operational error: %s", exc)
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except SQLAlchemyError:
                    logger.warning("Failed to close connection cleanly")

    def _validate_sql(self, sql: str) -> None:
        if not sql or not sql.strip():
            raise ValueError("SQL statement must be a non-empty string")

        query_type = _classify_query(sql)
        if self._read_only and query_type not in (
            SQLQueryType.SELECT,
            SQLQueryType.UNKNOWN,
        ):
            raise PermissionError(
                f"Write operation ({query_type.value}) is not allowed in read-only mode"
            )

    def _format_result(self, result: Result, query_type: SQLQueryType) -> list[dict[str, Any]]:
        columns = list(result.keys())
        rows = result.fetchall()
        limited = rows[: self._max_row_limit]
        if len(rows) > self._max_row_limit:
            logger.warning(
                "Result truncated: %d rows -> %d rows (max_row_limit=%d)",
                len(rows),
                self._max_row_limit,
                self._max_row_limit,
            )
        return [dict(zip(columns, row)) for row in limited], columns, len(rows)

    def execute(
        self,
        sql: str,
        params: Optional[dict[str, Any] | Sequence[Any]] = None,
    ) -> SQLResult:
        import time

        query_type = _classify_query(sql)
        start = time.monotonic()

        try:
            self._validate_sql(sql)

            with self._connection() as conn:
                stmt = text(sql)
                if params is not None:
                    result = conn.execute(stmt, params)
                else:
                    result = conn.execute(stmt)

                if query_type == SQLQueryType.SELECT:
                    data, columns, rowcount = self._format_result(result, query_type)
                else:
                    data = []
                    columns = []
                    rowcount = result.rowcount

                conn.commit()
                elapsed = (time.monotonic() - start) * 1000

                logger.info(
                    "SQL executed: type=%s, rows=%d, time=%.1fms",
                    query_type.value,
                    rowcount,
                    elapsed,
                )

                return SQLResult(
                    success=True,
                    data=data,
                    rowcount=rowcount,
                    columns=columns,
                    query_type=query_type,
                    execution_time_ms=elapsed,
                )

        except ProgrammingError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"SQL syntax error: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except (OperationalError, InterfaceError) as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"Database connection/operational error: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except DBAPIError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"Database API error: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except ArgumentError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"Invalid SQL argument: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except SQLAlchemyError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"SQLAlchemy error: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except ValueError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"Parameter error: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except PermissionError as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = str(exc)
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = f"Unexpected error: {type(exc).__name__}: {exc}"
            logger.error(msg)
            return SQLResult(
                success=False,
                error_message=msg,
                query_type=query_type,
                execution_time_ms=elapsed,
            )

    def test_connection(self) -> bool:
        try:
            with self._connection() as conn:
                if self._dialect in (SQLDialect.MYSQL, SQLDialect.MARIADB):
                    conn.execute(text("SELECT 1"))
                elif self._dialect == SQLDialect.POSTGRESQL:
                    conn.execute(text("SELECT 1"))
                elif self._dialect == SQLDialect.SQLITE:
                    conn.execute(text("SELECT 1"))
                elif self._dialect == SQLDialect.MSSQL:
                    conn.execute(text("SELECT 1"))
                elif self._dialect == SQLDialect.ORACLE:
                    conn.execute(text("SELECT 1 FROM DUAL"))
                else:
                    conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            logger.error("Connection test failed: %s", exc)
            return False

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.dispose()
                logger.info("Engine disposed successfully")
            except SQLAlchemyError as exc:
                logger.warning("Error disposing engine: %s", exc)
            finally:
                self._engine = None

    def __enter__(self) -> "SQLExecutor":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def execute_sql(
    sql: str,
    database_url: str,
    *,
    params: Optional[dict[str, Any] | Sequence[Any]] = None,
    read_only: bool = False,
    max_row_limit: int = 10000,
) -> SQLResult:
    with SQLExecutor(
        database_url,
        read_only=read_only,
        max_row_limit=max_row_limit,
    ) as executor:
        return executor.execute(sql, params=params)

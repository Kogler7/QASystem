from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import asyncio
import json as json_module
import logging
import os
import sys
import threading
import time
import concurrent.futures
from importlib.metadata import version
from typing import Any

logger = logging.getLogger(__name__)


def _transport_httpx():
    if int(version("mcp").split(".")[0]) >= 2:
        import httpx2
        return httpx2
    import httpx
    return httpx


def _normalize_windows_stdio_command(
    command: str, args: list[str] | None, env: dict[str, str] | None
) -> tuple[str, list[str], dict[str, str]]:
    """Windows 平台下对非可执行文件的命令做 cmd /c 包装。

    Args:
        command: 要执行的命令（如 ``python``、``node``）。
        args: 命令参数列表，可为 None。
        env: 环境变量字典，可为 None。

    Returns:
        ``(command, args, env)`` 三元组。在 Windows 且 command 不以 .exe/.cmd/.bat
        结尾时，会将原 command 塞入 args[0]，command 重写为 ``"cmd"`` 并前置 ``"/c"``。
    """
    if sys.platform == "win32" and command and not command.lower().endswith(
        (".exe", ".cmd", ".bat")
    ):
        args = [command, *(args or [])]
        command = "cmd"
        args = ["/c", *args]
    return command, args or [], env or {}


def _merge_stdio_env(env: dict[str, str] | None) -> dict[str, str]:
    """Merge stdio server env overrides with the current process env."""
    return {**os.environ, **(env or {})}


async def connect_single_server(
    name: str, cfg: dict[str, Any]
) -> tuple[str, Any, Any]:
    """连接单个 MCP 服务器，支持 stdio / sse / streamable_http 三种传输。

    使用 ``AsyncExitStack`` 管理所有异步上下文（传输层 + ClientSession），
    异常时自动释放已分配资源。

    Args:
        name: 服务器名称（用于日志和后续索引）。
        cfg: 服务器配置字典，需包含 ``transport`` 字段：
            - ``"stdio"`` — 需要 ``command``，可选 ``args``、``env``、``cwd``。
            - ``"sse"`` — 需要 ``url``，可选 ``headers``。
            - ``"streamable_http"`` / ``"http"`` — 需要 ``url``，可选 ``headers``。

    Returns:
        ``(name, stack, session, http_client)``：
        - *stack* — ``AsyncExitStack`` 实例，持有所有传输层上下文。
        - *session* — 已调用 ``initialize()`` 的 ``ClientSession``。
        - *http_client* — streamable HTTP 使用的 ``httpx.AsyncClient``，其它传输为 ``None``。
        连接失败时返回 ``(name, None, None, None)``。
    """
    from contextlib import AsyncExitStack

    from mcp import ClientSession

    stack = AsyncExitStack()
    await stack.__aenter__()

    try:
        http_client = None
        transport = (cfg.get("transport") or "streamable_http").strip().lower()

        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            command, args, env = _normalize_windows_stdio_command(
                cfg.get("command") or "",
                cfg.get("args", []),
                cfg.get("env"),
            )
            if not command:
                raise ValueError("stdio transport requires 'command' field")
            env = _merge_stdio_env(env)
            params_kwargs: dict[str, Any] = {"command": command, "args": args, "env": env}
            if cfg.get("cwd"):
                params_kwargs["cwd"] = cfg["cwd"]
            try:
                params = StdioServerParameters(**params_kwargs)
            except TypeError:
                # 旧版 mcp SDK 的 StdioServerParameters 不支持 cwd，降级不带 cwd。
                params_kwargs.pop("cwd", None)
                params = StdioServerParameters(**params_kwargs)
            read, write = await stack.enter_async_context(stdio_client(params))

        elif transport == "sse":
            httpx = _transport_httpx()
            from mcp.client.sse import sse_client

            url = (cfg.get("url") or "").strip()
            if not url:
                raise ValueError("sse transport requires 'url' field")

            def _httpx_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                """构造 SSE 专用 httpx 客户端，合并配置和调用方传入的 headers。

                Accept 头固定为 ``application/json, text/event-stream``，满足 SSE 协议要求。
                """
                merged_headers = {
                    "Accept": "application/json, text/event-stream",
                    **(cfg.get("headers") or {}),
                    **(headers or {}),
                }
                return httpx.AsyncClient(
                    headers=merged_headers or None,
                    follow_redirects=True,
                    timeout=timeout,
                    auth=auth,
                )

            read, write = await stack.enter_async_context(
                sse_client(url, httpx_client_factory=_httpx_client_factory)
            )

        elif transport in ("streamable_http", "http"):
            httpx = _transport_httpx()
            from mcp.client.streamable_http import streamable_http_client

            url = (cfg.get("url") or "").strip()
            if not url:
                raise ValueError(f"{transport} transport requires 'url' field")
            # 必须给传输层超时：timeout=None 会让"死会话"永久挂住，
            # 只能靠工具级超时兜底（表现就是"工具调用超时"）。
            http_timeout = float(config_getenv("MCP_HTTP_TIMEOUT_SECONDS", "30"))
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=cfg.get("headers") or None,
                    follow_redirects=True,
                    timeout=httpx.Timeout(http_timeout, read=None),
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )
            read, write = streams[:2]

        else:
            logger.warning(
                "MCP server '%s': unknown transport '%s'", name, transport
            )
            await stack.aclose()
            return name, None, None, None

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        logger.info("MCP server '%s' connected via %s", name, transport)
        return name, stack, session, http_client

    except Exception:
        logger.exception("MCP server '%s': connection failed", name)
        await stack.aclose()
        return name, None, None, None


def _unpack_connection(item: Any) -> tuple[str, Any, Any, Any]:
    if not isinstance(item, tuple) or len(item) < 3:
        name = item[0] if isinstance(item, tuple) and item else "unknown"
        return str(name), None, None, None
    name, stack, session = item[0], item[1], item[2]
    http_client = item[3] if len(item) > 3 else None
    return name, stack, session, http_client


_USER_HEADER_MAP = {
    "authorization": "Authorization",
    "x-user-id": "X-User-Id",
    "x-user-name": "X-User-Name",
    "x-tenant-id": "X-Tenant-Id",
    "x-user-roles": "X-User-Roles",
}


def _canonical_user_headers(headers: dict[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, value in headers.items():
        name = _USER_HEADER_MAP.get(key.lower())
        if name and value:
            canonical[name] = value
    return canonical


class MCPToolWrapper:
    """MCP 工具包装器：管理多 MCP 服务器的连接、工具注册和调用。

    内部维护一个专属的 ``asyncio`` 事件循环（``self._loop``），所有 session 的
    生命周期绑定在该循环上。工具调用通过 ``_make_handler`` 生成的闭包实现，
    确保每次调用复用同一个事件循环，避免跨循环访问导致的挂起。

    Attributes:
        _stacks: ``{server_name: AsyncExitStack}``，持有每个服务器的传输层上下文。
        _sessions: ``{server_name: ClientSession}``，已初始化的 MCP 会话。
        _tool_schemas: ``{tool_name: schema_dict}``，注册的工具描述。
        _tool_handlers: ``{tool_name: callable}``，工具调用闭包。
        _loop: 本 wrapper 专属的事件循环。
    """

    def __init__(self) -> None:
        self._stacks: dict[str, Any] = {}
        self._sessions: dict[str, Any] = {}
        self._tool_schemas: dict[str, dict] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._http_clients: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        # ── 身份绑定 / 会话重建（anyio cancel scope 有任务亲和性，见 _own_sessions）──
        self._servers: list[Any] = []
        self._bound_headers: dict[str, str] = {}
        self._identity_bound = False
        self._dirty = False
        self._owner_task: Any = None
        self._owner_stop: Any = None
        self._owner_gen = 0
        self._lifecycle_lock = threading.RLock()
        self._rebind_timeout = float(config_getenv("MCP_REBIND_TIMEOUT_SECONDS", "60"))
        self._user_header_names = (
            "Authorization",
            "X-User-Id",
            "X-User-Name",
            "X-Tenant-Id",
            "X-User-Roles",
        )

    def connect_all(self, servers: list[Any]) -> None:
        """并行连接所有配置的 MCP 服务器。

        为每个服务器创建独立的事件循环，使用 ``asyncio.gather`` 并发握手。
        单个服务器失败不影响其他服务器的连接和工具注册。

        Args:
            servers: ``McpServerConfig`` 列表，每个至少包含 ``name`` 和 ``transport`` 字段。
        """
        if not servers:
            logger.info("No MCP servers configured")
            return

        # 保存原始 server 配置，供身份切换时按新身份重建连接使用。
        self._servers = list(servers)

        loop = asyncio.new_event_loop()
        self._loop = loop
        self._loop_thread = threading.Thread(
            target=loop.run_forever,
            daemon=True,
            name="agentcode-mcp-loop",
        )
        self._loop_thread.start()
        # 首次连接（匿名）：只为拿到工具清单；真正的用户身份在 bind_identity() 时建立。
        self._rebind({})

    # ─────────────────── 身份绑定与会话重建 ───────────────────

    def _build_server_config(self, srv: Any, identity: dict[str, str]) -> tuple[str, dict[str, Any]]:
        """按给定身份构造连接配置；调用方身份合并进 cfg['headers']。

        身份必须体现在建连（initialize）请求上：MCP 服务端的工具是在
        "会话建立那一刻"的上下文中执行的，只给后续 tool call 加头是无效的。
        """
        cfg: dict[str, Any] = {"transport": srv.transport}
        if srv.url:
            cfg["url"] = srv.url
        if srv.command:
            cfg["command"] = srv.command
        if srv.args:
            cfg["args"] = srv.args
        if srv.env:
            cfg["env"] = srv.env
        headers = dict(srv.headers or {})
        if srv.auth_ref:
            token = config_getenv(srv.auth_ref)
            if token:
                headers.setdefault("Authorization", f"Bearer {token}")
        headers.update(identity)          # 调用方身份优先
        if headers:
            cfg["headers"] = headers
        return srv.name, cfg

    async def _own_sessions(self, gen: int, identity: dict[str, str], ready: Any, stop: Any) -> None:
        """在同一个任务里建立并销毁全部 MCP 连接。

        anyio 的 cancel scope 只能在进入它的任务里退出（否则报
        "Attempted to exit cancel scope in a different task"），而 MCP 客户端
        内部正是 anyio.create_task_group()。因此这里刻意：
          - 串行 await 每个 server（不用 asyncio.gather，它会各自建 Task，
            导致连接栈的进入/退出不在同一任务）；
          - 建连后不返回，一直 await stop，销毁也在本任务内完成；
          - 每条连接先 list_tools() 成功后才发布，绝不把"半死会话"暴露给
            工具调用（否则调用方会一直挂到超时）；
          - 只有仍是当前 owner（gen 未变）时才有权清空共享状态。
        """
        own: list[tuple[str, Any]] = []
        try:
            for srv in self._servers:
                name, cfg = self._build_server_config(srv, identity)
                try:
                    item = await connect_single_server(name, cfg)
                except BaseException as exc:            # noqa: BLE001
                    logger.error("MCP server %s: connection failed (%s): %s",
                                 name, type(exc).__name__, exc)
                    continue
                sname, stack, session, http_client = _unpack_connection(item)
                if stack is None or session is None:
                    logger.error("MCP server %s: connection failed", sname)
                    continue
                if gen != self._owner_gen:
                    # 已被更新的 owner 取代：丢弃这条连接，且不碰共享状态
                    await self._close_stack(sname, stack)
                    continue
                try:
                    tools = await session.list_tools()
                except BaseException as exc:            # noqa: BLE001
                    logger.error("MCP server %s: list_tools failed: %s", sname, exc)
                    await self._close_stack(sname, stack)
                    continue
                own.append((sname, stack))
                with self._lifecycle_lock:
                    self._stacks[sname] = stack
                    self._sessions[sname] = session
                    if http_client is not None:
                        self._http_clients.append(http_client)
                for mcp_tool in tools.tools:
                    tool_name = f"{sname}__{mcp_tool.name}"
                    self._tool_schemas[tool_name] = self._convert_schema(tool_name, mcp_tool)
                    self._tool_handlers[tool_name] = self._make_handler(sname, mcp_tool.name)
                logger.info("MCP server %s: connected, %d tools (identity=%s, gen=%d)",
                            sname, len(tools.tools), sorted(identity) or "none", gen)
            if not ready.done():
                ready.set_result(True)
            await stop.wait()
        except BaseException as exc:                    # noqa: BLE001
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            # 清理必须发生在本任务内，而且只清自己创建的那批
            for sname, stack in own:
                await self._close_stack(sname, stack)
            with self._lifecycle_lock:
                if gen == self._owner_gen:
                    self._stacks.clear()
                    self._sessions.clear()
                    self._http_clients.clear()

    async def _close_stack(self, name: str, stack: Any) -> None:
        try:
            await stack.aclose()
        except BaseException as exc:                    # noqa: BLE001
            logger.warning("Error closing stack for %s: %s", name, exc)

    async def _stop_owner(self) -> None:
        """通知旧 owner 任务退出（栈在它自己的任务里销毁），并等待其结束。

        先把代次 +1：旧 owner 从此不再有资格清空共享状态，避免它的 finally
        晚到一步、把新 owner 刚发布的会话清掉。
        """
        self._owner_gen += 1
        task, stop = self._owner_task, self._owner_stop
        self._owner_task = None
        self._owner_stop = None
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except BaseException as exc:                    # noqa: BLE001
            logger.warning("MCP owner task stop failed: %s", exc)
            task.cancel()

    async def _rebind_async(self, identity: dict[str, str]) -> None:
        await self._stop_owner()
        # 原子化：重建窗口内不暴露任何旧/半死会话，工具调用会快速失败而非挂住
        with self._lifecycle_lock:
            self._stacks.clear()
            self._sessions.clear()
            self._http_clients.clear()
        gen = self._owner_gen
        self._owner_stop = asyncio.Event()
        ready = self._loop.create_future()
        self._owner_task = self._loop.create_task(
            self._own_sessions(gen, identity, ready, self._owner_stop)
        )
        try:
            await asyncio.wait_for(ready, timeout=self._rebind_timeout)
        except BaseException:
            await self._stop_owner()
            raise

    def _rebind(self, identity: dict[str, str]) -> None:
        """在专属事件循环上重建全部 MCP 会话（同步等待）。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("MCP event loop is not running")
        try:
            asyncio.run_coroutine_threadsafe(
                self._rebind_async(identity), loop
            ).result(timeout=self._rebind_timeout + 10)
        except Exception as exc:                        # noqa: BLE001
            self._dirty = True                          # 保持脏，下一个任务再试
            raise RuntimeError(f"MCP 会话按新身份重建失败: {exc}") from exc
        self._dirty = False

    def bind_identity(self, headers: dict[str, str] | None) -> None:
        """为本任务绑定调用方身份；身份变化时立即重建会话（fail-closed）。

        在任务开始（借用 ToolManager 后）调用，早于任何工具调用，避免并发
        工具调用各自触发重连。重建失败会向上抛出：任务不应在无身份时继续。
        """
        canonical = _canonical_user_headers(headers or {})
        with self._lifecycle_lock:
            self._identity_bound = True
            if canonical == self._bound_headers and not self._dirty:
                return
            self._bound_headers = canonical
            self._dirty = True
        self._rebind(canonical)

    def _invalidate_session(self, server_name: str, reason: str) -> None:
        """超时/取消后把该 server 的会话标记为不可用。

        被 cancel 的 call_tool 可能让会话停在"有请求没回包"的状态，继续复用会
        让后续调用持续排队直到超时；这里直接丢弃，由下一个任务按身份重建。
        """
        with self._lifecycle_lock:
            self._stacks.pop(server_name, None)
            self._sessions.pop(server_name, None)
            self._dirty = True
        logger.warning("MCP 会话已失效: server=%s reason=%s（下一个任务将按身份重建）",
                       server_name, reason)

    def end_task(self) -> None:
        """任务结束：只失效标记，保留 _bound_headers 供下一个同身份任务复用。"""
        with self._lifecycle_lock:
            self._identity_bound = False

    def _convert_schema(self, tool_name: str, mcp_tool: Any) -> dict:
        """将 MCP SDK 返回的 Tool 对象转换为 agent 内部使用的工具描述格式。

        Args:
            tool_name: 全局工具名，格式为 ``"{server_name}__{local_name}"``。
            mcp_tool: MCP SDK ``list_tools()`` 返回的 ``Tool`` 对象。

        Returns:
            ``{"name": ..., "description": ..., "input_schema": ...}`` 字典。
            ``input_schema`` 来自 Tool 的 ``inputSchema`` 属性。
        """
        schema = getattr(mcp_tool, "input_schema", None)
        if schema is None:
            schema = getattr(mcp_tool, "inputSchema", None)
        return {
            "name": tool_name,
            "description": mcp_tool.description or mcp_tool.name,
            "input_schema": schema or {},
        }

    def _make_handler(self, server_name: str, tool_name: str):
        """为指定 MCP 工具创建一个可跨线程调用的闭包 handler。

        闭包在调用时通过 ``self._loop`` 执行 ``session.call_tool()`` 协程，
        确保与连接建立时使用同一个事件循环，避免 ``asyncio.run()`` 新建循环
        导致的跨循环挂起问题。

        Args:
            server_name: MCP 服务器名称（如 ``"qasystem-tools"``）。
            tool_name: 该服务器上的本地工具名（原始名，不含 server 前缀）。

        Returns:
            一个签名为 ``(**kwargs) -> str`` 的可调用对象。
        """
        def handler(**kwargs: Any) -> str:
            timeout_s = max(0.1, float(kwargs.pop("_agent_timeout_s", 60)))
            cancel_token = kwargs.pop("_agent_cancel_token", None)
            # fail-closed：本任务未绑定身份 / 会话正在重建时，绝不复用上一个身份
            # 的会话（否则会出现跨调用方的越权）。
            if not self._identity_bound:
                return f"Error: MCP identity not bound for this task (server '{server_name}')"
            if self._dirty:
                return f"Error: MCP session for '{server_name}' is being rebound, retry later"
            session = self._sessions.get(server_name)
            if not session:
                return f"Error: MCP server '{server_name}' is not connected"
            started = time.monotonic()
            try:
                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        session.call_tool(tool_name, kwargs), self._loop
                    )
                    deadline = self._loop.time() + timeout_s
                    while True:
                        if cancel_token is not None and cancel_token.is_cancelled():
                            future.cancel()
                            self._invalidate_session(server_name, "cancelled")
                            return "Error: Task cancelled"
                        remaining = deadline - self._loop.time()
                        if remaining <= 0:
                            future.cancel()
                            self._invalidate_session(server_name, "timeout")
                            elapsed = time.monotonic() - started
                            logger.warning(
                                "MCP 工具超时: server=%s tool=%s timeout=%.0fs elapsed=%.0fs "
                                "identity_bound=%s dirty=%s sessions=%s",
                                server_name, tool_name, timeout_s, elapsed,
                                self._identity_bound, self._dirty, sorted(self._sessions),
                            )
                            return (f"Error: MCP tool timed out after {timeout_s:g}s "
                                    f"(elapsed={elapsed:.0f}s)")
                        try:
                            result = future.result(timeout=min(0.2, remaining))
                            break
                        except concurrent.futures.TimeoutError:
                            continue
                elif self._loop:
                    result = self._loop.run_until_complete(
                        asyncio.wait_for(session.call_tool(tool_name, kwargs), timeout_s)
                    )
                else:
                    result = asyncio.run(session.call_tool(tool_name, kwargs))
                return _format_mcp_result(result)
            except Exception as e:
                logger.exception(
                    "MCP call '%s.%s' failed", server_name, tool_name
                )
                return f"Error calling MCP tool '{tool_name}': {e}"

        return handler

    def get_tool_schemas(self) -> list[dict]:
        """获取所有已注册工具的 schema 列表，供 LLM function calling 使用。

        Returns:
            工具描述字典列表，每个元素形如
            ``{"name": "server__tool", "description": "...", "input_schema": {...}}``。
        """
        return list(self._tool_schemas.values())

    def get_tool_definitions(self) -> dict[str, dict]:
        """获取工具名 → schema 的映射字典。

        Returns:
            ``{tool_name: schema_dict}`` 的浅拷贝。
        """
        return dict(self._tool_schemas)

    def get_tool_handlers(self) -> dict[str, Any]:
        """获取工具名 → handler 可调用对象的映射字典。

        Returns:
            ``{tool_name: callable}`` 的浅拷贝。
        """
        return dict(self._tool_handlers)

    def get_session(self, server_name: str) -> Any:
        """按服务器名获取已建立的 MCP ClientSession。

        Args:
            server_name: 服务器名称。

        Returns:
            ``ClientSession`` 实例，未连接时返回 ``None``。
        """
        return self._sessions.get(server_name)

    def disconnect_all(self) -> None:
        """断开所有 MCP 服务器连接并释放资源。

        依次关闭每个服务器的 ``AsyncExitStack``（含传输层和 ClientSession），
        最后关闭专属事件循环 ``self._loop``。
        """
        async def _disconnect() -> None:
            for name, stack in list(self._stacks.items()):
                try:
                    await stack.aclose()
                    logger.info("MCP server '%s' disconnected", name)
                except Exception:
                    logger.exception(
                        "Error disconnecting MCP server '%s'", name
                    )

        try:
            if self._loop and self._stacks:
                if self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
                    future.result(timeout=30)
                else:
                    self._loop.run_until_complete(_disconnect())
        except Exception:
            logger.exception("Error during MCP disconnect")
        finally:
            self._server_owner_shutdown()
            self._stacks.clear()
            self._sessions.clear()
            self._http_clients.clear()
            self._tool_schemas.clear()
            self._tool_handlers.clear()
            self._servers = []
            self._bound_headers = {}
            self._dirty = False
            self._identity_bound = False
            if self._loop:
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                if self._loop_thread is not None:
                    self._loop_thread.join(timeout=5)
                self._loop.close()
                self._loop = None
                self._loop_thread = None


    def _server_owner_shutdown(self) -> None:
        """关闭 owner 任务：连接栈必须由持有它的任务自己退出。

        anyio 的 cancel scope 只能在其所属任务内退出，所以这里不能直接
        aclose() 连接栈，而是通知 owner 任务结束（它会在自己的 finally 里清理）。
        """
        loop = self._loop
        if loop is None or self._owner_task is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._stop_owner(), loop).result(timeout=20)
        except Exception as exc:                        # noqa: BLE001
            logger.warning("MCP owner shutdown failed: %s", exc)


def _format_mcp_result(result: Any) -> str:
    """将 MCP 工具调用返回的 ``CallToolResult`` 格式化为人类可读字符串。

    处理规则：
    - ``None`` → ``"(no output)"``
    - 有 ``.content`` 属性 → 遍历列表，拼接 ``text`` / JSON(data) / ``[mimeType]``
    - 其他 → ``str(result)``

    Args:
        result: ``session.call_tool()`` 返回的原始结果。

    Returns:
        格式化后的字符串，可直接注入 LLM 对话上下文。
    """
    if result is None:
        return "(no output)"
    if hasattr(result, "content"):
        parts: list[str] = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "data"):
                parts.append(
                    json_module.dumps(item.data, ensure_ascii=False)
                )
            elif hasattr(item, "mime_type") or hasattr(item, "mimeType"):
                parts.append(f"[{getattr(item, 'mime_type', getattr(item, 'mimeType', ''))}]")
        return "\n".join(parts) if parts else str(result)
    return str(result)

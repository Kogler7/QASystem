from __future__ import annotations

import fnmatch
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from .errors import ErrorCode
from .interfaces import ToolAdapter
from .models import SessionConfig


class HttpToolAdapter:
    """Minimal MCP JSON-RPC adapter: tools/list and tools/call."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        with httpx.Client(timeout=30) as client:
            resp = client.post(self.url, json=payload, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("result", {})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments, "timeout_s": timeout_s})
        if not isinstance(result, dict):
            raise ValueError("MCP call result must be a dict")
        return result


class McpClient:
    def __init__(self, config: SessionConfig, adapters: dict[str, ToolAdapter] | None = None) -> None:
        self.config = config
        self.adapters: dict[str, ToolAdapter] = adapters or {}
        for server in config.mcp_servers:
            if server.name in self.adapters:
                continue
            if server.transport == "http" and server.url:
                self.adapters[server.name] = HttpToolAdapter(server.url)
                continue
            raise ValueError(f"Unsupported MCP transport for server={server.name}")
        self._tool_map: dict[str, tuple[str, str]] = {}

    def list_tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        self._tool_map = {}
        for server_name, adapter in self.adapters.items():
            for tool in adapter.list_tools():
                local_name = tool["name"]
                full_name = f"{server_name}__{local_name}"
                self._tool_map[full_name] = (server_name, local_name)
                out.append({**tool, "name": full_name})
        return out

    @staticmethod
    def is_tool_allowed(tool_name: str, allowlist: list[str]) -> bool:
        if not allowlist:
            return True
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in allowlist)

    def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout_s: int, retries: int) -> dict[str, Any]:
        if tool_name not in self._tool_map:
            return {"ok": False, "error_code": ErrorCode.TOOL_UNAVAILABLE.value, "error": "Unknown tool"}
        server_name, local_name = self._tool_map[tool_name]
        adapter = self.adapters[server_name]
        attempts = retries + 1
        last_error = ""
        for _ in range(attempts):
            try:
                result = adapter.call_tool(local_name, arguments, timeout_s)
                return {"ok": True, "content": result}
            except httpx.TimeoutException as exc:
                last_error = str(exc)
                code = ErrorCode.TOOL_TIMEOUT.value
            except (ValueError, TypeError) as exc:
                last_error = str(exc)
                code = ErrorCode.TOOL_BAD_RESPONSE.value
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                code = ErrorCode.MCP_PROTOCOL_ERROR.value
        return {"ok": False, "error_code": code, "error": last_error}

    def call_many(
        self,
        calls: list[dict[str, Any]],
        timeout_s: int,
        retries: int,
        execution_mode: str = "sequential",
    ) -> list[dict[str, Any]]:
        if execution_mode == "parallel" and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=len(calls)) as executor:
                futures = [
                    executor.submit(
                        self.call_tool,
                        call["name"],
                        call.get("input", {}),
                        timeout_s,
                        retries,
                    )
                    for call in calls
                ]
                results = [future.result() for future in futures]
        else:
            results = [
                self.call_tool(call["name"], call.get("input", {}), timeout_s, retries) for call in calls
            ]
        mapped: list[dict[str, Any]] = []
        for call, result in zip(calls, results):
            mapped.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "name": call["name"],
                    "is_error": not result.get("ok", False),
                    "content": result.get("content") if result.get("ok", False) else None,
                    "error_code": result.get("error_code"),
                    "error": result.get("error"),
                }
            )
        return mapped

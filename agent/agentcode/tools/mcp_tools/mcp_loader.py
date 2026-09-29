"""
从 mcp_config.json 批量加载 MCP 服务配置。

文件格式示例：

{
  "mcpServers": {
    "remote-streamable-http": {
      "transport": "streamable_http",
      "url": "http://example.com/mcp"
    },
    "local-tool": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/server"
      }
    },
    "remote-sse": {
      "transport": "sse",
      "url": "https://example.com/sse",
      "headers": {
        "Authorization": "Bearer sk-xxx"
      }
    }
  }
}

transport 取值与必需字段：
  - streamable_http / http:必需 url
  - stdio:必需 command, 可选 args、env
  - sse:必需 url, 可选 headers
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ...models import McpServerConfig, SessionConfig, SkillRef, BudgetConfig, ToolPolicy, DeliveryConfig


def load_mcp_servers(config_path: str | Path) -> list[McpServerConfig]:
    """读取 mcp_config.json，返回 McpServerConfig 列表。

    每个 key 对应一个 MCP 服务名，其 value 至少需含 "transport" 和 "url" 字段。
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"MCP 配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)

    mcp_servers: dict[str, Any] = data.get("mcpServers") or data.get("mcp_servers") or {}
    if not mcp_servers:
        raise ValueError(f"MCP 配置文件中未找到 mcpServers 字段: {config_path}")

    servers: list[McpServerConfig] = []
    for name, cfg in mcp_servers.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"MCP 服务 {name} 配置格式错误，应为 dict")

        transport = (cfg.get("transport") or "streamable_http").strip().lower()
        if transport not in ("http", "streamable_http", "stdio", "sse"):
            raise ValueError(f"MCP 服务 {name} 不支持的 transport: {transport}")

        url = (cfg.get("url") or "").strip() or None
        if len(mcp_servers) == 1 and os.getenv("AGENT_MCP_URL"):
            url = os.environ["AGENT_MCP_URL"].strip()
        command = (cfg.get("command") or "").strip() or None
        if transport == "stdio" and not command:
            raise ValueError(f"MCP 服务 {name} 缺少 command 字段")
        if transport != "stdio" and not url:
            raise ValueError(f"MCP 服务 {name} 缺少 url 字段")

        servers.append(
            McpServerConfig(
                name=name,
                transport=transport,
                url=url,
                command=command,
                args=cfg.get("args") or [],
                env=cfg.get("env") or {},
                headers=cfg.get("headers") or {},
                auth_ref=cfg.get("auth_ref"),
            )
        )

    return servers


def build_config_from_mcp_json(
    config_path: str | Path,
    *,
    session_id: str | None = None,
    trace_id: str | None = None,
    skills: list[SkillRef] | None = None,
    tool_policy: ToolPolicy | None = None,
    budget: BudgetConfig | None = None,
    delivery: DeliveryConfig | None = None,
) -> SessionConfig:
    """从 mcp_config.json 一次性构建 SessionConfig。

    只提供 MCP 服务列表，其余字段（session_id、trace_id 等）若未传入则使用默认值。
    """
    import time
    import uuid

    now_ms = int(time.time() * 1000)

    return SessionConfig(
        session_id=session_id or f"mcp-session-{now_ms}",
        trace_id=trace_id or str(uuid.uuid4()),
        mcp_servers=load_mcp_servers(config_path),
        skills=skills or [],
        tool_policy=tool_policy or ToolPolicy(allow=[]),
        budget=budget or BudgetConfig(),
        delivery=delivery,
    )

"""
北京时间 MCP（标准库 HTTP + JSON-RPC）。

暴露方法：`tools/list`、`tools/call`。工具本地名 `get_beijing_time`；
在 `SessionConfig.mcp_servers` 中 server `name` 为 `time` 时，运行时全名为 `time__get_beijing_time`。
"""

from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from zoneinfo import ZoneInfo

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_beijing_time",
        "description": "返回当前 Asia/Shanghai（北京时间）。Return current Asia/Shanghai time.",
        "input_schema": {"type": "object", "properties": {}},
    }
]


def _beijing_now_payload() -> dict[str, Any]:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    return {
        "timezone": "Asia/Shanghai",
        "beijing_time_iso": now.isoformat(),
        "beijing_time_display": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def _handle_rpc(body: dict[str, Any]) -> dict[str, Any]:
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        if name != "get_beijing_time":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool: {name}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": _beijing_now_payload()}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


class _McpHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        if self.path != "/mcp" and not self.path.startswith("/mcp?"):
            self.send_error(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
                ).encode("utf-8")
            )
            return

        out = _handle_rpc(body)
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> HTTPServer:
    return HTTPServer((host, port), _McpHandler)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Beijing time MCP (HTTP JSON-RPC)")
    p.add_argument("--host", default=_DEFAULT_HOST)
    p.add_argument("--port", type=int, default=_DEFAULT_PORT)
    args = p.parse_args()
    httpd = serve(args.host, args.port)
    print(f"Beijing time MCP listening on http://{args.host}:{args.port}/mcp")
    httpd.serve_forever()


if __name__ == "__main__":
    main()


"""Real wire-format smoke test, runnable with both supported MCP SDK majors."""
import json
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from mcp_services.runtime import MCPServer, MCP_V2
from external_services.application import capture_user_headers, reset_user_headers, get_user_headers


def test_mcp_http_initialize_list_and_call_preserve_wire_contract():
    server = MCPServer("compatibility-test")

    @server.tool()
    def echo(value: str) -> str:
        assert get_user_headers().get("authorization") == "Bearer compatibility-test"
        return value

    mounted = server.streamable_http_app(**({"host": "0.0.0.0"} if MCP_V2 else {}))

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=mounted)], lifespan=lifespan)

    async def bind_identity(request, call_next):
        token = capture_user_headers(dict(request.headers))
        try:
            return await call_next(request)
        finally:
            reset_user_headers(token)

    app.add_middleware(BaseHTTPMiddleware, dispatch=bind_identity)

    headers = {"Accept": "application/json, text/event-stream",
               "Authorization": "Bearer compatibility-test"}

    def post(client, method, params, request_id):
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert response.status_code == 200, response.text
        if response.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
        if "text/event-stream" in response.headers.get("content-type", ""):
            payload = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
        else:
            payload = response.json()
        assert "error" not in payload, payload
        return payload["result"]

    with TestClient(app, base_url="http://localhost:8890") as client:
        result = post(client, "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "qasystem-test", "version": "1"}}, 1)
        headers["mcp-protocol-version"] = result["protocolVersion"]
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        assert response.status_code in (200, 202, 204)
        tools = post(client, "tools/list", {}, 2)["tools"]
        tool = next(t for t in tools if t["name"] == "echo")
        assert tool["inputSchema"]["required"] == ["value"]
        result = post(client, "tools/call", {"name": "echo", "arguments": {"value": "兼容测试"}}, 3)
        assert not result.get("isError", False)
        assert result["content"][0]["text"] == "兼容测试"

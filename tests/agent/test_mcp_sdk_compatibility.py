import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agentcode.tools.mcp_tools import mcp as client


@pytest.mark.parametrize("field", ["input_schema", "inputSchema"])
def test_tool_schema_is_preserved_across_sdk_versions(field):
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    tool = SimpleNamespace(name="search", description="Search", **{field: schema})
    result = client.MCPToolWrapper()._convert_schema("tools__search", tool)
    assert result["input_schema"] == schema


@pytest.mark.parametrize("sdk_version,module", [("1.27.0", "httpx"), ("2.0.0", "httpx2")])
def test_transport_uses_sdk_http_client_types(monkeypatch, sdk_version, module):
    import sys
    fake = SimpleNamespace()
    monkeypatch.setitem(sys.modules, module, fake)
    monkeypatch.setattr(client, "version", lambda _: sdk_version)
    assert client._transport_httpx() is fake


@pytest.mark.parametrize("stream_count", [2, 3])
def test_http_transport_preserves_identity_and_closes_session(monkeypatch, stream_count):
    import mcp
    import mcp.client.streamable_http as transport
    events = []
    identity = {"Authorization": "Bearer test-only-value"}

    class HTTPClient:
        def __init__(self, **kwargs):
            assert kwargs["headers"] == identity
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            events.append("http_closed")

    @asynccontextmanager
    async def streams(url, http_client):
        assert url == "http://test/mcp"
        yield tuple(range(stream_count))
        events.append("transport_closed")

    class Session:
        def __init__(self, read, write):
            assert (read, write) == (0, 1)
        async def __aenter__(self):
            return self
        async def initialize(self):
            events.append("initialized")
        async def __aexit__(self, *args):
            events.append("session_closed")

    monkeypatch.setattr(client, "_transport_httpx", lambda: SimpleNamespace(
        AsyncClient=HTTPClient, Timeout=lambda *args, **kwargs: None))
    monkeypatch.setattr(transport, "streamable_http_client", streams)
    monkeypatch.setattr(mcp, "ClientSession", Session)

    async def run():
        name, stack, session, http = await client.connect_single_server(
            "test", {"url": "http://test/mcp", "headers": identity})
        assert name == "test" and session is not None and http is not None
        await stack.aclose()
    asyncio.run(run())
    assert events == ["initialized", "session_closed", "transport_closed", "http_closed"]

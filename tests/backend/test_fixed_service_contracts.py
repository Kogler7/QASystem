from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("module_name", ["mcp_services.domains.data.sql", "agent_mcp.data"])
def test_sql_uses_only_canonical_relational_connection(monkeypatch, module_name):
    import importlib
    from algorithm.data_access import sql
    module = importlib.import_module(module_name)
    calls = []
    monkeypatch.setattr(sql, "SQLExecutor", lambda url, **kwargs: calls.append((url, kwargs)) or "executor")
    monkeypatch.setenv("MCP_DATABASE_URL", "mysql://retired")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///canonical.db")
    assert module._executor() == "executor"
    assert calls[-1][0] == "sqlite:///canonical.db"
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ValueError, match="关系型 SQL"):
        module._executor()
    assert module._executor("sqlite:///explicit.db") == "executor"
    assert calls[-1][0] == "sqlite:///explicit.db"


def test_standalone_mcp_stdio_is_an_explicit_startup_argument(monkeypatch):
    from mcp_services import server
    calls = []
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    monkeypatch.setattr(server, "mcp", SimpleNamespace(run=lambda **kwargs: calls.append(kwargs)))
    server.main(["--transport", "stdio"])
    assert calls == [{"transport": "stdio"}]


def test_standalone_mcp_http_binding_can_be_explicitly_set(monkeypatch):
    from mcp_services import server
    calls = []
    fake = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs), settings=SimpleNamespace())
    monkeypatch.setattr(server, "mcp", fake)
    monkeypatch.setenv("MCP_HOST", "unsafe-host")
    monkeypatch.setenv("MCP_PORT", "1234")
    server.main(["--host", "127.0.0.1", "--port", "9981"])
    assert calls[0]["transport"] == "streamable-http"
    if server.MCP_V2:
        assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 9981
    else:
        assert fake.settings.host == "127.0.0.1" and fake.settings.port == 9981


def test_term_index_cannot_use_legacy_direct_graph_connection(monkeypatch):
    from algorithm.term_vector import build
    from external_services.application import ExternalCapabilityUnavailable, bind_user_headers
    for key in ("TERM_GRAPH_HTTP_URL", "TERM_GRAPH_USER", "TERM_GRAPH_PASSWORD", "TERM_GRAPH_DB"):
        monkeypatch.setenv(key, "forbidden-direct-connection")
    with bind_user_headers({}):
        with pytest.raises(ExternalCapabilityUnavailable, match="用户授权上下文"):
            build.fetch_terms(10, "Term")


def test_term_index_queries_use_shared_authenticated_queue(monkeypatch):
    from algorithm.term_vector import build
    from backend.app.execution import manager
    from external_services.application import bind_user_headers
    calls = []
    class FakeManager:
        is_async = True
        def submit(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(job_id="job")
        def wait_blocking(self, job_id, timeout):
            return {"status": "succeeded"}
        def result(self, job_id):
            return [{"id": "term-1", "name": "甲"}]
    monkeypatch.setattr(manager, "get_execution_manager", lambda: FakeManager())
    with bind_user_headers({"authorization": "Bearer private"}):
        assert build.fetch_terms(10, "Term")[0]["id"] == "term-1"
    assert calls[0]["operation"] == "term_vector.query"
    assert calls[0]["resource_class"] == "io.db"
    assert "private" not in str(calls)


def test_term_query_worker_requires_authorization(monkeypatch):
    from backend.app.execution.operations import _term_vector_query
    from external_services.application import bind_user_headers, ExternalCapabilityUnavailable
    with bind_user_headers({}):
        with pytest.raises(ExternalCapabilityUnavailable):
            _term_vector_query({"statement": "MATCH (n) RETURN n"})


def test_term_index_build_preserves_explicit_cli_inputs(monkeypatch, tmp_path):
    import json
    from algorithm.term_vector import build
    received = []
    monkeypatch.setenv("TERM_VECTOR_INDEX", str(tmp_path / "retired.json"))
    monkeypatch.setenv("TERM_RESOURCE_TYPE", "Retired")
    def fetch(limit, term_type):
        received.append(term_type)
        return [{"id": "one", "name": "术语", "definition": "定义"}]
    monkeypatch.setattr(build, "fetch_terms", fetch)
    monkeypatch.setattr(build, "batch_text_to_vectors", lambda values: [[1.0, 0.0] for _ in values])
    output = tmp_path / "new" / "index.json"
    build.main(["--output", str(output), "--term-type", "Custom", "--no-logical"])
    assert received == ["Custom"]
    assert json.loads(output.read_text(encoding="utf-8"))["records"][0]["id"] == "one"
    assert not (tmp_path / "retired.json").exists()


@pytest.mark.parametrize("container,agent,mcp", [
    (True, "http://agent:9180", "http://agent-mcp:9181/mcp"),
    (False, "http://127.0.0.1:9180", "http://127.0.0.1:9181/mcp"),
])
def test_internal_agent_connections_follow_topology(monkeypatch, container, agent, mcp):
    from docker import startup_contract
    monkeypatch.setattr(startup_contract, "in_container_runtime", lambda: container)
    assert startup_contract.internal_agent_url() == agent
    assert startup_contract.internal_agent_mcp_url() == mcp


def test_sql_health_unconfigured_or_sqlite_never_opens_network(monkeypatch):
    from backend.app import health
    monkeypatch.setattr(health.socket, "create_connection", lambda *args, **kwargs: pytest.fail("unexpected TCP connection"))
    assert health._sql_database_check("")["skipped"]
    assert health._sql_database_check("sqlite:///db.sqlite")["skipped"]


@pytest.mark.parametrize("url,host,port", [
    ("mysql+pymysql://user:secret@mysql.example/database", "mysql.example", 3306),
    ("postgresql://user:secret@postgres.example:5444/db", "postgres.example", 5444),
    ("mssql+pyodbc://user:secret@sql.example/db", "sql.example", 1433),
])
def test_sql_health_uses_actual_connection_target_without_secrets(monkeypatch, url, host, port):
    from backend.app import health
    calls = []
    monkeypatch.setattr(health, "_tcp_check", lambda name, hostname, number: calls.append((hostname, number)) or {"ok": True})
    result = health._sql_database_check(url)
    assert result["ok"] and calls == [(host, port)]
    assert "secret" not in str(result)


@pytest.mark.parametrize("url", ["malformed", "mysql://user:secret@host:invalid/db", "mysql:///db"])
def test_invalid_sql_health_never_tries_wrong_network_target(monkeypatch, url):
    from backend.app import health
    monkeypatch.setattr(health.socket, "create_connection", lambda *args, **kwargs: pytest.fail("unexpected TCP connection"))
    result = health._sql_database_check(url)
    assert not result["ok"] and "secret" not in str(result)

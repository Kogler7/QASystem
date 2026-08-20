import asyncio
import json
import sys
import types
from pathlib import Path

from mcp_services.server import mcp
from mcp_services.domains.data import sql as sql_tools
from mcp_services.domains.knowledge import extraction as knowledge_tools
from mcp_services.domains.graph import locator as locator_tools
from mcp_services.domains.graph import retrieval as graph_tools
from mcp_services.domains.visualization import charts as chart_tools


EXPECTED_TOOLS = {
    "draw_area_chart",
    "draw_bar_chart",
    "draw_box_plot",
    "draw_bubble_chart",
    "draw_heatmap",
    "draw_histogram",
    "draw_line_chart",
    "draw_pie_chart",
    "draw_radar_chart",
    "draw_scatter_plot",
    "execute_sql",
    "get_job",
    "get_job_result",
    "kg_extract_v2",
    "kg_get_dimension_scales",
    "kg_init_knowledge_space",
    "kg_store_extraction",
    "cancel_job",
    "link_entities",
    "list_chart_types",
    "locate_knowledge_space",
    "get_all_subspaces",
    "get_stakeholders",
    "query_entity_properties",
    "search_graph",
}


def test_unified_mcp_registers_expected_tools():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_agentcode_uses_single_mcp_server():
    root = Path(__file__).resolve().parents[2]
    config_path = root / "agent" / "agentcode" / "tools" / "mcp_tools" / "mcp_config.json"
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers == {
        "qasystem-tools": {
            "transport": "streamable_http",
            "url": "http://127.0.0.1:8000/mcp",
        }
    }


def assert_envelope(result):
    assert set(result) == {"code", "message", "data", "error"}


def test_mcp_static_tools_use_standard_envelope():
    chart_result = chart_tools.list_chart_types()
    assert_envelope(chart_result)
    assert chart_result["code"] == 0


def test_visualization_failure_uses_standard_envelope(monkeypatch):
    fake = types.SimpleNamespace(
        bar_chart=lambda _data: {"success": False, "error": "labels 与 values 长度不一致"}
    )
    monkeypatch.setattr(chart_tools, "_executor", lambda: fake)
    result = chart_tools.draw_bar_chart(["A"], [1, 2])
    assert_envelope(result)
    assert result["code"] == 3400
    assert result["error"]["details"]["error"] == "labels 与 values 长度不一致"


def test_knowledge_tool_wraps_algorithm_failure(monkeypatch):
    fake_module = types.ModuleType("algorithm.knowledge_extract")
    fake_module.extract_v2 = lambda **_kwargs: {
        "_error": "JSON 解析失败",
        "error_code": "LLM_JSON_PARSE_FAILED",
    }
    monkeypatch.setitem(sys.modules, "algorithm.knowledge_extract", fake_module)
    result = knowledge_tools.extract_knowledge("ontology", "text")
    assert_envelope(result)
    assert result["code"] == 3100


def test_sql_tool_wraps_executor_result(monkeypatch):
    class Result:
        def to_dict(self):
            return {"success": True, "data": [{"value": 1}], "rowcount": 1}

    class Executor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(sql_tools, "_executor", lambda *_args, **_kwargs: Executor())
    result = sql_tools.execute_sql("SELECT 1")
    assert_envelope(result)
    assert result["code"] == 0
    assert result["data"]["rowcount"] == 1


def test_graph_locator_uses_standard_envelope(monkeypatch):
    fake = types.SimpleNamespace(
        locate=lambda question, requirement: {
            "question": question,
            "requirement": requirement,
            "target": "采购知识空间",
        }
    )
    monkeypatch.setattr(locator_tools, "_locator", lambda: fake)
    result = locator_tools.locate_knowledge_space("采购金额是多少", {"domain": "采购"})
    assert_envelope(result)
    assert result["code"] == 0
    assert result["data"]["target"] == "采购知识空间"


def test_graph_search_failure_uses_graph_error_code(monkeypatch):
    monkeypatch.setattr(
        graph_tools,
        "_search_neighbors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("application service unavailable")),
    )
    result = graph_tools.search_graph("entity-1")
    assert_envelope(result)
    assert result["code"] == 3200
    assert "application service unavailable" in result["error"]["details"]


def test_graph_search_passes_atomic_filters(monkeypatch):
    captured = {}

    def search(node_id, direction, limit, relationship_types):
        captured.update(
            node_id=node_id,
            direction=direction,
            limit=limit,
            relationship_types=relationship_types,
        )
        return {"depth": 1, "triples": [], "found_entities": [], "has_more": False}

    monkeypatch.setattr(graph_tools, "_search_neighbors", search)
    result = graph_tools.search_graph(
        "NEO_1",
        relationship_types=["HAS_SOLUTION"],
        direction="outgoing",
        limit=10,
    )
    assert result["code"] == 0
    assert captured == {
        "node_id": "NEO_1",
        "relationship_types": ["HAS_SOLUTION"],
        "direction": "outgoing",
        "limit": 10,
    }


def test_graph_search_rejects_multi_hop_request():
    result = graph_tools.search_graph("NEO_1", depth=2)
    assert result["code"] == 3000


def test_graph_write_tools_fail_as_unavailable_without_database_access():
    result = knowledge_tools.store_extraction({"nodes": [], "relationships": []})
    assert result["code"] == 3902
    assert result["error"]["details"]["capability"] == "graph.write"

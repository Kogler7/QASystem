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
    "describe_table",
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
    "get_database_info",
    "kg_extract_v2",
    "kg_get_dimension_scales",
    "kg_init_knowledge_space",
    "kg_store_extraction",
    "link_entities",
    "list_chart_types",
    "list_supported_dialects",
    "list_tables",
    "locate_knowledge_space",
    "get_all_subspaces",
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
    dialect_result = sql_tools.list_supported_dialects()
    assert_envelope(chart_result)
    assert_envelope(dialect_result)
    assert chart_result["code"] == 0
    assert dialect_result["code"] == 0


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
        "_graph_components",
        lambda: (_ for _ in ()).throw(RuntimeError("neo4j unavailable")),
    )
    result = graph_tools.search_graph("entity-1")
    assert_envelope(result)
    assert result["code"] == 3200
    assert "neo4j unavailable" in result["error"]["details"]


def test_graph_search_passes_atomic_filters(monkeypatch):
    class Client:
        def search_neighbors(self, entity_id, **filters):
            return {
                "depth": 1,
                "triples": [],
                "found_entities": [],
                "has_more": False,
                "entity_id": entity_id,
                "filters": filters,
            }

    wrapper = types.SimpleNamespace(client=Client())
    monkeypatch.setattr(graph_tools, "_graph_components", lambda: (wrapper, object()))
    result = graph_tools.search_graph(
        "NEO_1",
        relationship_types=["HAS_SOLUTION"],
        direction="outgoing",
        limit=10,
    )
    assert result["code"] == 0
    assert result["data"]["filters"] == {
        "relationship_types": ["HAS_SOLUTION"],
        "direction": "outgoing",
        "limit": 10,
    }


def test_graph_search_rejects_multi_hop_request():
    result = graph_tools.search_graph("NEO_1", depth=2)
    assert result["code"] == 3000


def test_neo4j_neighbor_search_preserves_edge_direction(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "neo4j",
        types.SimpleNamespace(GraphDatabase=types.SimpleNamespace()),
    )
    from algorithm.ans.QA.neo4j_client import Neo4jClient

    client = Neo4jClient.__new__(Neo4jClient)
    client.execute_cypher = lambda *_args, **_kwargs: [
        {
            "start_id": 1,
            "start_labels": ["ProblemClass"],
            "start_properties": {"name": "问题类"},
            "relationship_type": "HAS_SOLUTION",
            "relationship_properties": {"order": 1},
            "is_outgoing": True,
            "neighbor_id": 2,
            "neighbor_labels": ["SolutionClass"],
            "neighbor_properties": {"name": "方案类"},
        }
    ]
    result = client.search_neighbors(
        "NEO_1",
        relationship_types=["HAS_SOLUTION"],
        direction="outgoing",
        limit=10,
    )
    triple = result["triples"][0]
    assert triple["head_entity"]["id"] == "NEO_1"
    assert triple["tail_entity"]["id"] == "NEO_2"
    assert triple["relationship_properties"] == {"order": 1}

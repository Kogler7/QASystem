import asyncio
import json
import sys
import types
from pathlib import Path

from mcp_services.server import mcp
from mcp_services.domains.data import sql as sql_tools
from mcp_services.domains.knowledge import extraction as knowledge_tools
from mcp_services.domains.reasoning import answer as answer_tools
from mcp_services.domains.reasoning import intent as intent_tools
from mcp_services.domains.visualization import charts as chart_tools


EXPECTED_TOOLS = {
    "batch_recognize_intent",
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
    "get_all_subspaces",
    "get_database_info",
    "get_plan_from_intent",
    "get_reasoning_rules",
    "kg_extract_v2",
    "kg_get_dimension_scales",
    "kg_init_knowledge_space",
    "kg_store_extraction",
    "link_entities",
    "list_chart_types",
    "list_supported_dialects",
    "list_tables",
    "locate_knowledge_space",
    "query_entity_properties",
    "recognize_intent",
    "search_graph",
}


def test_unified_mcp_registers_expected_tools():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_agentcode_uses_single_mcp_server():
    root = Path(__file__).resolve().parents[2]
    config_path = root / "agentcode" / "agentcode" / "tools" / "mcp_tools" / "mcp_config.json"
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


def test_batch_intent_rejects_empty_requests_without_loading_models():
    result = intent_tools.batch_recognize_intent([])
    assert_envelope(result)
    assert result["code"] != 0
    assert result["error"] is not None


def test_missing_reasoning_rules_is_reported_not_raised():
    result = answer_tools.get_reasoning_rules()
    assert_envelope(result)
    assert result["code"] == 3900
    assert "尚未提供" in result["message"]


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

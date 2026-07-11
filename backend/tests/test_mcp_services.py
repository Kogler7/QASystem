import asyncio
import json
from pathlib import Path

from mcp_services.server import mcp


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

"""
MCP Server：知识抽取 V2

提供以下工具：
- kg_extract_v2：本体驱动的知识抽取
- kg_get_dimension_scales：查询维度刻度
- kg_init_knowledge_space：初始化知识空间
- kg_store_extraction：抽取结果存入 Neo4j

启动方式：
  python mcp_server.py
或在 Claude Code 配置：
  {
    "mcpServers": {
      "kg-extract-v2": {
        "command": "python",
        "args": ["algorithm/knowledge_extract/mcp_server.py"],
        "cwd": "/path/to/qasystem"
      }
    }
  }
"""
import json
import sys
import os
from pathlib import Path

# 确保 algorithm 在 sys.path 中
_ALGO_ROOT = Path(__file__).resolve().parent.parent
if str(_ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALGO_ROOT))

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from knowledge_extract import (
    extract_v2,
    get_dimension_scales,
    init_knowledge_space,
    store_extraction_to_neo4j,
)

server = Server("kg-extract-v2")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="kg_extract_v2",
            description="本体驱动的知识抽取（V2）。输入 PUML 本体定义和待抽取文本，返回结构化实体和关系。",
            inputSchema={
                "type": "object",
                "properties": {
                    "ontology": {
                        "type": "string",
                        "description": "PlantUML 本体定义（class 图）",
                    },
                    "text": {
                        "type": "string",
                        "description": "待抽取知识的中文文本",
                    },
                    "model": {
                        "type": "string",
                        "description": "LLM 模型名（可选，默认使用环境变量配置）",
                    },
                    "base_url": {
                        "type": "string",
                        "description": "LLM API 地址（可选，默认使用环境变量配置）",
                    },
                },
                "required": ["ontology", "text"],
            },
        ),
        types.Tool(
            name="kg_get_dimension_scales",
            description="查询指定维度下的所有刻度。输入维度名称，返回该维度关联的刻度列表。",
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": "维度名称，如：时间、空间、抽象层级",
                    },
                },
                "required": ["dimension"],
            },
        ),
        types.Tool(
            name="kg_init_knowledge_space",
            description="初始化知识空间。输入维度-刻度映射字典，创建 KnowledgeSpace 节点并连接刻度。",
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension_scales": {
                        "type": "object",
                        "description": '维度-刻度映射，如 {"时间": ["年月"], "空间": ["集团", "子公司"]}',
                    },
                },
                "required": ["dimension_scales"],
            },
        ),
        types.Tool(
            name="kg_store_extraction",
            description="将知识抽取结果的 graph 结构存入 Neo4j（按 uid 去重，同一标识符的节点不重复创建）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "graph": {
                        "type": "object",
                        "description": '抽取结果的 graph 字段，包含 nodes 和 relationships',
                    },
                },
                "required": ["graph"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    try:
        if name == "kg_extract_v2":
            result = extract_v2(
                ontology=arguments.get("ontology", ""),
                text=arguments.get("text", ""),
                model=arguments.get("model"),
                base_url=arguments.get("base_url"),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]

        elif name == "kg_get_dimension_scales":
            result = get_dimension_scales(
                dimension=arguments.get("dimension", "")
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]

        elif name == "kg_init_knowledge_space":
            result = init_knowledge_space(
                dimension_scales=arguments.get("dimension_scales", {})
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]

        elif name == "kg_store_extraction":
            result = store_extraction_to_neo4j(
                graph=arguments.get("graph", {})
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e)}, ensure_ascii=False)
        )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

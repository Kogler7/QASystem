from __future__ import annotations

"""Development MCP server for graph retrieval contracts.

This server is intentionally small. It documents the MCP surface expected by the
new Agentic GraphRAG system and can be replaced by adapters around existing
Neo4j / NL2Cypher / QA modules.
"""

import json

try:
    from fastmcp.server import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Install optional dependency: pip install -e .[mcp]") from exc


app = FastMCP(name="graph-retrieval-contracts", version="0.1.0")


@app.tool(name="locate_knowledge_space")
def locate_knowledge_space(question: str) -> dict:
    if "采购" in question:
        space = "采购管理域-信息"
    elif "故障" in question or "列车" in question:
        space = "运维诊断域-规则"
    else:
        space = "通用知识空间"
    return {"knowledge_space": space, "confidence": 0.7, "reason": "contract mock"}


@app.tool(name="link_entities")
def link_entities(question: str, keywords_json: str | None = None) -> dict:
    keywords = []
    if keywords_json:
        try:
            keywords = json.loads(keywords_json)
        except json.JSONDecodeError:
            keywords = [keywords_json]
    return {
        "candidates": [
            {
                "id": "MOCK_ENTITY",
                "name": keywords[0] if keywords else "候选实体",
                "score": 0.5,
                "labels": ["Mock"],
                "properties": {},
            }
        ]
    }


@app.tool(name="search_graph")
def search_graph(start_entity_id: str, question: str = "", depth: int = 1) -> dict:
    return {
        "paths": [
            {
                "path_id": f"mock_path_{start_entity_id}_{depth}",
                "nodes": [{"id": start_entity_id}, {"id": "MOCK_NEIGHBOR"}],
                "relationships": [{"from": start_entity_id, "type": "RELATED_TO", "to": "MOCK_NEIGHBOR"}],
                "score": 0.5,
            }
        ],
        "has_more": False,
    }


if __name__ == "__main__":
    app.run(transport="streamable-http")

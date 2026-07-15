from __future__ import annotations

import json
import re
from typing import Any


def md_to_text(markdown_content: str) -> str:
    text = markdown_content or ""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`.*?`", "", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[([^\]]*?)\]\([^)]*?\)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*+]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*|__(.*?)__|\*(.*?)\*|_(.*?)_", lambda match: next(value for value in match.groups() if value is not None), text)
    return re.sub(r"\n\s*\n\s*\n", "\n\n", text).strip()


def normalize_document_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2, default=str)


def build_graphs_response(graph_or_items: dict[str, Any] | list[dict[str, Any]]) -> dict[str, list[Any]]:
    if isinstance(graph_or_items, list):
        return {
            "graphs": [
                {
                    "graph": {
                        "index": str(item.get("index", index)),
                        "nodes": (item.get("graph") or {}).get("nodes", []),
                        "relationships": (item.get("graph") or {}).get("relationships", []),
                    }
                }
                for index, item in enumerate(graph_or_items)
            ]
        }
    graph = graph_or_items or {}
    if not (graph.get("nodes") or graph.get("relationships")):
        return {"graphs": []}
    return {
        "graphs": [
            {
                "graph": {
                    "index": "0",
                    "nodes": graph.get("nodes", []),
                    "relationships": graph.get("relationships", []),
                }
            }
        ]
    }


def generate_understanding(graph: dict[str, Any]) -> str:
    nodes = graph.get("nodes", []) if graph else []
    relationships = graph.get("relationships", []) if graph else []
    if not nodes:
        return "未抽取到知识节点"
    labels: dict[str, int] = {}
    for node in nodes:
        label = node.get("label", "Unknown")
        labels[label] = labels.get(label, 0) + 1
    summary = "、".join(f"{count} 个 {label}" for label, count in labels.items())
    return f"共抽取 {len(nodes)} 个节点（{summary}）和 {len(relationships)} 条关系。"
